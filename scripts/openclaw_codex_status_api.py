#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path('/Users/sunyujing/litellm-gateway')
SCRIPT_DIR = ROOT / 'scripts'
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import sync_codex_oauth_test_env as sync  # noqa: E402
import query_openclaw_codex_quota as quota_mod  # noqa: E402

ENV_PATH = ROOT / '.env'
WATCHER_STATE_PATH = ROOT / 'scripts' / '.watch_openclaw_codex_profile_state.json'
LITELLM_HEALTH_URL = 'http://127.0.0.1:4002/health/liveliness'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 4010


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_from_millis(ms: str | int | None) -> str | None:
    try:
        value = int(ms or 0)
    except Exception:
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()


def parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip()
    return env


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def token_fingerprint(token: str) -> str:
    if not token:
        return ''
    return hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]


def fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 5) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', 200)
            body = resp.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = body
        return {'ok': True, 'status': status, 'body': parsed}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        return {'ok': False, 'status': exc.code, 'error': body[:1000]}
    except Exception as exc:
        return {'ok': False, 'status': 0, 'error': str(exc)}


def format_summary_text(summary: dict[str, Any]) -> str:
    email = str(summary.get('envBoundEmail') or summary.get('resolvedProfileEmail') or summary.get('chromeCurrentEmail') or '-')
    plan = str(summary.get('envBoundPlanType') or '-')
    healthy = 'healthy' if summary.get('litellmHealthy') else 'unhealthy'
    resync = 'yes' if summary.get('shouldResyncLiteLLM') else 'no'
    five_hour = '-'
    seven_day = '-'
    for window in summary.get('quotaWindows') or []:
        name = str(window.get('name') or '')
        remaining = window.get('remainingPercent')
        used = window.get('usedPercent')
        reset = str(window.get('resetAtIso') or '-')
        compact = f"remain={remaining}% used={used}% reset={reset}"
        if name == 'five_hour':
            five_hour = compact
        elif name == 'seven_day':
            seven_day = compact
    return f"account={email} | plan={plan} | litellm={healthy} | resync={resync} | five_hour[{five_hour}] | seven_day[{seven_day}]"


def list_candidate_profiles(profiles: dict[str, dict]) -> list[tuple[str, dict]]:
    return sync.list_candidate_profiles(profiles)


def find_profile_by_email(profiles: dict[str, dict], email: str) -> tuple[str, dict] | None:
    normalized = email.strip().lower()
    if not normalized:
        return None
    matches = [
        (profile_id, profile)
        for profile_id, profile in list_candidate_profiles(profiles)
        if str(profile.get('email') or '').strip().lower() == normalized
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            int(item[1].get('expires') or 0),
            1 if str(item[1].get('refresh') or '').strip() else 0,
            item[0],
        ),
        reverse=True,
    )
    return matches[0]


def build_status() -> dict[str, Any]:
    env = parse_env(ENV_PATH)
    watcher_state = load_json(WATCHER_STATE_PATH)
    chrome = sync.get_chrome_account()
    profiles = sync.load_profiles()

    chrome_email = str(chrome.get('email') or '').strip() if chrome.get('ok') else ''
    strict_match = find_profile_by_email(profiles, chrome_email) if chrome_email else None

    try:
        resolved_profile_id, resolved_profile, resolved_source = sync.choose_profile(
            profiles,
            chrome_email=chrome_email,
            allow_nonmatching_fallback=True,
        )
        resolved = {
            'profileId': resolved_profile_id,
            'email': str(resolved_profile.get('email') or '').strip(),
            'accountId': str(resolved_profile.get('accountId') or '').strip(),
            'planType': str(resolved_profile.get('chatgptPlanType') or '').strip(),
            'expires': str(resolved_profile.get('expires') or '').strip(),
            'expiresAtIso': iso_from_millis(resolved_profile.get('expires')),
            'selectionSource': resolved_source,
            'accessFingerprint': token_fingerprint(str(resolved_profile.get('access') or '').strip()),
        }
    except Exception as exc:
        resolved = {
            'profileId': '',
            'email': '',
            'accountId': '',
            'planType': '',
            'expires': '',
            'expiresAtIso': None,
            'selectionSource': 'unresolved',
            'error': str(exc),
            'accessFingerprint': '',
        }

    env_oauth_access = env.get('OAUTH_UPSTREAM_API_KEY', '')
    env_account_id = env.get('OAUTH_UPSTREAM_ACCOUNT_ID', '')
    env_email = env.get('OAUTH_UPSTREAM_EMAIL', '')
    env_plan_type = env.get('OAUTH_UPSTREAM_PLAN_TYPE', '')
    env_expires = env.get('OAUTH_UPSTREAM_EXPIRES', '')

    quota = quota_mod.fetch_quota(env_oauth_access, env_account_id) if env_oauth_access else {
        'ok': False,
        'status': 0,
        'error': 'missing_env_oauth_access_token',
    }

    litellm_health = fetch_json(LITELLM_HEALTH_URL, timeout=5)

    strict_match_dict = None
    if strict_match:
        strict_match_dict = {
            'profileId': strict_match[0],
            'email': str(strict_match[1].get('email') or '').strip(),
            'accountId': str(strict_match[1].get('accountId') or '').strip(),
            'planType': str(strict_match[1].get('chatgptPlanType') or '').strip(),
            'expires': str(strict_match[1].get('expires') or '').strip(),
            'expiresAtIso': iso_from_millis(strict_match[1].get('expires')),
            'accessFingerprint': token_fingerprint(str(strict_match[1].get('access') or '').strip()),
        }

    env_binding = {
        'baseUrl': env.get('OAUTH_UPSTREAM_BASE_URL', ''),
        'email': env_email,
        'accountId': env_account_id,
        'planType': env_plan_type,
        'expires': env_expires,
        'expiresAtIso': iso_from_millis(env_expires),
        'accessFingerprint': token_fingerprint(env_oauth_access),
    }

    consistency = {
        'chromeToResolvedEmailMatch': bool(chrome_email and chrome_email.lower() == str(resolved.get('email') or '').strip().lower()),
        'chromeToEnvEmailMatch': bool(chrome_email and env_email and chrome_email.lower() == env_email.lower()),
        'resolvedToEnvEmailMatch': bool(env_email and str(resolved.get('email') or '').strip() and env_email.lower() == str(resolved.get('email') or '').strip().lower()),
        'resolvedToEnvAccountIdMatch': bool(env_account_id and str(resolved.get('accountId') or '').strip() and env_account_id == str(resolved.get('accountId') or '').strip()),
        'resolvedToEnvAccessFingerprintMatch': bool(env_oauth_access and str(resolved.get('accessFingerprint') or '').strip() and token_fingerprint(env_oauth_access) == str(resolved.get('accessFingerprint') or '').strip()),
        'watcherToEnvEmailMatch': bool(str(((watcher_state.get('selectedEmail') or ''))).strip() and env_email and str(watcher_state.get('selectedEmail')).strip().lower() == env_email.lower()),
    }
    consistency['shouldResyncLiteLLM'] = not (
        consistency['resolvedToEnvEmailMatch']
        and consistency['resolvedToEnvAccountIdMatch']
        and consistency['resolvedToEnvAccessFingerprintMatch']
    )

    quota_windows = quota.get('windows') if isinstance(quota, dict) else []
    summary = {
        'chromeCurrentEmail': chrome_email,
        'resolvedProfileEmail': str(resolved.get('email') or '').strip(),
        'envBoundEmail': env_email,
        'envBoundPlanType': env_plan_type,
        'quotaWindows': quota_windows,
        'shouldResyncLiteLLM': consistency['shouldResyncLiteLLM'],
        'litellmHealthy': bool(litellm_health.get('ok')),
    }
    summary['text'] = format_summary_text(summary)

    return {
        'ok': True,
        'kind': 'openclaw-codex-status',
        'generatedAt': now_iso(),
        'chrome': {
            'ok': bool(chrome.get('ok')),
            'email': chrome_email,
            'name': str(chrome.get('name') or '').strip(),
            'selectedProfileId': str(chrome.get('selectedProfileId') or '').strip(),
            'selectedProfileName': str(chrome.get('selectedProfileName') or '').strip(),
            'selectedGoogleAccount': str(chrome.get('selectedGoogleAccount') or '').strip(),
            'selectionSource': str(chrome.get('selectionSource') or '').strip(),
            'error': '' if chrome.get('ok') else str(chrome.get('error') or ''),
            'profiles': chrome.get('profiles') if isinstance(chrome.get('profiles'), list) else [],
        },
        'openclaw': {
            'strictEmailMatchProfile': strict_match_dict,
            'resolvedProfile': resolved,
            'candidateProfileCount': len(list_candidate_profiles(profiles)),
        },
        'litellmBinding': env_binding,
        'watcher': watcher_state,
        'quota': quota,
        'litellm': {
            'healthUrl': LITELLM_HEALTH_URL,
            'health': litellm_health,
        },
        'consistency': consistency,
        'summary': summary,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ('/', '/status'):
            self._send_json(build_status())
            return
        if self.path == '/summary':
            status = build_status()
            self._send_json(status.get('summary') if isinstance(status.get('summary'), dict) else {'ok': False, 'error': 'summary_unavailable'})
            return
        if self.path == '/summary.txt':
            status = build_status()
            text = str(((status.get('summary') or {}).get('text')) or '')
            body = (text + '\n').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/healthz':
            status = build_status()
            ok = bool(status.get('litellm', {}).get('health', {}).get('ok'))
            self._send_json({'ok': ok, 'generatedAt': status.get('generatedAt'), 'shouldResyncLiteLLM': status.get('consistency', {}).get('shouldResyncLiteLLM')}, 200 if ok else 503)
            return
        if self.path == '/quota':
            status = build_status()
            self._send_json(status.get('quota') if isinstance(status.get('quota'), dict) else {'ok': False, 'error': 'quota_unavailable'})
            return
        self._send_json({'ok': False, 'error': 'not_found', 'path': self.path}, 404)


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f'openclaw-codex-status-api listening on http://{host}:{port}', flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description='Expose local JSON status for Chrome -> OpenClaw Codex OAuth -> LiteLLM binding and official Plus quota.')
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--serve', action='store_true', help='Run HTTP server instead of printing one JSON snapshot.')
    parser.add_argument('--summary', action='store_true', help='Print only the human-friendly summary text.')
    args = parser.parse_args()

    if args.serve:
        serve(args.host, args.port)
    else:
        status = build_status()
        if args.summary:
            print(((status.get('summary') or {}).get('text')) or '')
        else:
            print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
