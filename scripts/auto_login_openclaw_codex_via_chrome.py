#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT / 'scripts' / 'sync_litellm_from_openclaw_codex.sh'
PREFERRED_ACCOUNT_JSON = ROOT / 'tmp' / 'openai_plus_account_extracted.json'
CHROME_HELPER = ROOT / 'scripts' / 'get_chrome_chatgpt_account.js'
URL_RE = re.compile(r'Open:\s+(https://\S+)')
MANUAL_RE = re.compile(r'Manual OAuth entry required|Paste the authorization code', re.I)
SUCCESS_RE = re.compile(r'Saved auth profile|Authenticated|Login complete|oauth', re.I)
RETRY_BUTTON_COORDS = [(960, 671), (960, 646)]
EMAIL_INPUT_COORDS = [(960, 372), (960, 347)]
EMAIL_CONTINUE_COORDS = [(960, 448), (960, 423)]
OTP_LOGIN_COORDS = [(960, 686), (960, 661)]
CALLBACK_PREFIX = 'http://localhost:1455/auth/callback'


def resolve_node_binary() -> str:
    for candidate in (shutil.which('node'), '/opt/homebrew/bin/node', '/usr/local/bin/node'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return 'node'


def get_default_email() -> str:
    if PREFERRED_ACCOUNT_JSON.exists():
        try:
            data = json.loads(PREFERRED_ACCOUNT_JSON.read_text(encoding='utf-8'))
            email = str(data.get('email') or '').strip()
            if email:
                return email
        except Exception:
            pass
    try:
        proc = subprocess.run([resolve_node_binary(), str(CHROME_HELPER)], cwd=str(ROOT), capture_output=True, text=True, check=False)
        raw = (proc.stdout or '').strip()
        if raw:
            data = json.loads(raw)
            email = str(data.get('email') or '').strip()
            if email:
                return email
    except Exception:
        pass
    return str(os.environ.get('OPENAI_CODEX_LOGIN_EMAIL') or '').strip()


def open_in_system_google_chrome(url: str) -> None:
    script = f'''
    tell application "Google Chrome"
        activate
        open location "{url}"
    end tell
    '''
    subprocess.run(['osascript', '-e', script], check=True)


def get_system_chrome_active_tab() -> tuple[str, str]:
    script = '''
    tell application "Google Chrome"
        if (count of windows) = 0 then
            return "\n"
        end if
        return (title of active tab of front window) & "\n" & (URL of active tab of front window)
    end tell
    '''
    result = subprocess.run(['osascript', '-e', script], check=True, capture_output=True, text=True)
    text = result.stdout.strip()
    if '\n' in text:
        title, url = text.split('\n', 1)
    else:
        title, url = text, ''
    return title.strip(), url.strip()


def get_chrome_window_title() -> str:
    result = subprocess.run(
        ['peekaboo', 'list', 'windows', '--app', 'Google Chrome', '--json'],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    windows = data.get('data', {}).get('windows', [])
    if not windows:
        return ''
    return str((windows[0] or {}).get('title') or '')


def click_chrome_coords(x: int, y: int) -> None:
    subprocess.run(
        ['peekaboo', 'click', '--app', 'Google Chrome', '--coords', f'{x},{y}'],
        check=True,
        capture_output=True,
        text=True,
    )


def click_coord_candidates(candidates: list[tuple[int, int]]) -> None:
    last_error: Exception | None = None
    for x, y in candidates:
        try:
            click_chrome_coords(x, y)
            return
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


def get_callback_url() -> str | None:
    try:
        _, url = get_system_chrome_active_tab()
    except Exception:
        return None
    if url.startswith(CALLBACK_PREFIX) or ('code=' in url and 'state=' in url and 'auth/callback' in url):
        return url
    return None


def maybe_drive_system_chrome(opened_at: float, state: dict, email: str) -> None:
    try:
        _, url = get_system_chrome_active_tab()
    except Exception:
        url = ''
    try:
        window_title = get_chrome_window_title()
    except Exception:
        window_title = ''

    elapsed = time.time() - opened_at
    now = time.time()
    if ('糟糕，出错了！' in window_title or '身份验证错误' in window_title) and now - state.get('lastRetryClickAt', 0) > 3:
        click_coord_candidates(RETRY_BUTTON_COORDS)
        state['lastRetryClickAt'] = now
        return
    if '欢迎回来 - OpenAI' in window_title and elapsed > 2 and not state.get('emailSubmitted'):
        click_coord_candidates(EMAIL_INPUT_COORDS)
        subprocess.run(['peekaboo', 'type', email, '--app', 'Google Chrome'], check=True, capture_output=True, text=True)
        click_coord_candidates(EMAIL_CONTINUE_COORDS)
        state['emailSubmitted'] = 1
        return
    if '输入密码 - OpenAI' in window_title and not state.get('otpRequested'):
        click_coord_candidates(OTP_LOGIN_COORDS)
        state['otpRequested'] = 1
        return
    if url.startswith(CALLBACK_PREFIX):
        state['callbackSeen'] = 1


def try_submit_manual_callback(fd: int, opened_at: float, state: dict, email: str, wait_seconds: int = 20) -> bool:
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        callback_url = get_callback_url()
        if callback_url:
            os.write(fd, callback_url.encode('utf-8') + b'\n')
            return True
        try:
            maybe_drive_system_chrome(opened_at, state, email)
        except Exception:
            pass
        time.sleep(0.5)
    return False


def read_until_exit(cmd: list[str], timeout_seconds: int, email: str) -> tuple[int, str, str | None, bool]:
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)

    output = ''
    auth_url: str | None = None
    opened = False
    sent_enter = False
    manual_required = False
    start = time.time()
    opened_at: float | None = None
    drive_state: dict[str, float] = {}

    try:
        while True:
            elapsed = time.time() - start
            if not sent_enter and elapsed > 2.0:
                os.write(fd, b'\r')
                sent_enter = True
            if elapsed > timeout_seconds:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                if drive_state.get('otpRequested') and not drive_state.get('callbackSeen'):
                    raise TimeoutError(
                        f'openclaw auth login reached OpenAI email verification and is now waiting for the OTP sent to {email}. '
                        f'Partial output:\n{output[-4000:]}'
                    )
                raise TimeoutError(f'openclaw auth login timed out after {timeout_seconds}s. Partial output:\n{output[-4000:]}')

            r, _, _ = select.select([fd], [], [], 0.2)
            if fd in r:
                try:
                    chunk = os.read(fd, 8192).decode('utf-8', errors='ignore')
                except OSError:
                    chunk = ''
                if chunk:
                    output += chunk
                    if auth_url is None:
                        m = URL_RE.search(output)
                        if m:
                            auth_url = m.group(1)
                    if auth_url and not opened:
                        open_in_system_google_chrome(auth_url)
                        opened = True
                        opened_at = time.time()
                    if MANUAL_RE.search(output):
                        manual_required = True
                        if opened and opened_at is not None and try_submit_manual_callback(fd, opened_at, drive_state, email):
                            manual_required = False
                            continue
                        os.kill(pid, signal.SIGTERM)
                        break

            if opened and opened_at is not None:
                try:
                    maybe_drive_system_chrome(opened_at, drive_state, email)
                except Exception:
                    pass

            ended_pid, status = os.waitpid(pid, os.WNOHANG)
            if ended_pid == pid:
                return os.waitstatus_to_exitcode(status), output, auth_url, manual_required
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), output, auth_url, manual_required


def main() -> None:
    parser = argparse.ArgumentParser(description='Use current Google Chrome session to complete OpenClaw Codex browser login, then sync LiteLLM.')
    parser.add_argument('--timeout-seconds', type=int, default=90)
    parser.add_argument('--email', default=get_default_email())
    parser.add_argument('--skip-sync', action='store_true')
    args = parser.parse_args()

    cmd = ['openclaw', 'models', 'auth', 'login', '--provider', 'openai-codex']
    code, output, auth_url, manual_required = read_until_exit(cmd, args.timeout_seconds, args.email)

    sys.stdout.write(output)
    sys.stdout.flush()

    if manual_required:
        raise SystemExit('OpenClaw login fell back to manual OAuth entry; current Chrome session did not complete callback automatically.')
    if code != 0:
        raise SystemExit(f'OpenClaw login exited with code {code}. Output:\n{output[-4000:]}')
    if not auth_url:
        raise SystemExit('OpenClaw login did not expose an OAuth URL; unable to verify browser-driven login path.')

    if not args.skip_sync:
        subprocess.run([str(SYNC_SCRIPT)], cwd=str(ROOT), check=True)
        print('Synced LiteLLM after OpenClaw Codex login.')


if __name__ == '__main__':
    main()
