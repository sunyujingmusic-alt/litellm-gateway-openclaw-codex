#!/usr/bin/env python3
from __future__ import annotations

import os
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_HOME = Path(os.environ.get('OPENCLAW_HOME') or (Path.home() / '.openclaw'))
AUTH_JSON = OPENCLAW_HOME / 'agents' / 'main' / 'agent' / 'auth-profiles.json'
STATE_PATH = ROOT / 'scripts' / '.watch_openclaw_codex_profile_state.json'
SYNC_SCRIPT = ROOT / 'scripts' / 'sync_litellm_from_openclaw_codex.sh'
CHROME_HELPER = ROOT / 'scripts' / 'get_chrome_chatgpt_account.js'
AUTO_LOGIN_SCRIPT = ROOT / 'scripts' / 'auto_login_openclaw_codex_via_chrome.py'
AUTO_LOGIN_COOLDOWN_SECONDS = 15 * 60


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> int:
    return int(time.time())


def load_profiles() -> dict[str, dict]:
    data = json.loads(AUTH_JSON.read_text(encoding='utf-8'))
    profiles = data.get('profiles') if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        raise SystemExit('auth-profiles.json has unexpected shape')
    return profiles


def list_candidate_profiles(profiles: dict[str, dict]) -> list[tuple[str, dict]]:
    candidates: list[tuple[str, dict]] = []
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if profile.get('provider') != 'openai-codex':
            continue
        access = str(profile.get('access') or '').strip()
        if not access:
            continue
        candidates.append((profile_id, profile))
    return candidates


def choose_profile(profiles: dict[str, dict], preferred_email: str = '') -> tuple[str, dict] | None:
    candidates = list_candidate_profiles(profiles)
    if not candidates:
        return None

    if preferred_email:
        email_matches = [
            (profile_id, profile)
            for profile_id, profile in candidates
            if str(profile.get('email') or '').strip().lower() == preferred_email.lower()
        ]
        if email_matches:
            email_matches.sort(
                key=lambda item: (
                    int(item[1].get('expires') or 0),
                    1 if str(item[1].get('refresh') or '').strip() else 0,
                    item[0],
                ),
                reverse=True,
            )
            return email_matches[0]

    def score(item: tuple[str, dict]) -> tuple[int, int, str]:
        profile_id, profile = item
        expires = int(profile.get('expires') or 0)
        has_refresh = 1 if str(profile.get('refresh') or '').strip() else 0
        return (expires, has_refresh, profile_id)

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def compute_fingerprint(profile_id: str, profile: dict) -> str:
    access = str(profile.get('access') or '').strip()
    expires = str(profile.get('expires') or '').strip()
    email = str(profile.get('email') or '').strip()
    raw = '\n'.join([profile_id, access, expires, email])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def get_chrome_account() -> dict:
    proc = subprocess.run(['node', str(CHROME_HELPER)], cwd=str(ROOT), capture_output=True, text=True)
    raw = (proc.stdout or '').strip()
    if not raw:
        return {'ok': False, 'error': 'empty_stdout'}
    try:
        return json.loads(raw)
    except Exception:
        return {'ok': False, 'error': 'invalid_json', 'raw': raw[:500]}


def main() -> None:
    state = load_state()
    state['lastCheckedAt'] = now_iso()
    state['lastCheckedTs'] = now_ts()

    profiles = load_profiles()
    chrome = get_chrome_account()
    chrome_email = str(chrome.get('email') or '').strip() if chrome.get('ok') else ''
    selected = choose_profile(profiles, chrome_email)

    state['chromeAccount'] = {
        'ok': bool(chrome.get('ok')),
        'email': chrome_email,
        'name': str(chrome.get('name') or '').strip(),
        'error': '' if chrome.get('ok') else str(chrome.get('error') or ''),
        'selectedProfileId': str(chrome.get('selectedProfileId') or '').strip(),
        'selectedProfileName': str(chrome.get('selectedProfileName') or '').strip(),
        'selectedGoogleAccount': str(chrome.get('selectedGoogleAccount') or '').strip(),
        'selectionSource': str(chrome.get('selectionSource') or '').strip(),
    }

    if selected is None:
        state.update({
            'selectedProfile': '',
            'selectedEmail': '',
            'expires': 0,
            'status': 'no_profile',
        })
        if chrome_email:
            last_attempt_ts = int(state.get('lastAutoLoginAttemptTs') or 0)
            if now_ts() - last_attempt_ts >= AUTO_LOGIN_COOLDOWN_SECONDS:
                state['lastAutoLoginAttemptAt'] = now_iso()
                state['lastAutoLoginAttemptTs'] = now_ts()
                save_state(state)
                subprocess.run(['python3', str(AUTO_LOGIN_SCRIPT), '--email', chrome_email], cwd=str(ROOT), check=True)
                profiles = load_profiles()
                selected = choose_profile(profiles, chrome_email)
        if selected is None:
            save_state(state)
            print('no usable openai-codex profile found; nothing to sync')
            return

    profile_id, profile = selected
    openclaw_email = str(profile.get('email') or '').strip()
    fingerprint = compute_fingerprint(profile_id, profile)
    previous = str(state.get('fingerprint') or '')

    state.update({
        'selectedProfile': profile_id,
        'selectedEmail': openclaw_email,
        'expires': int(profile.get('expires') or 0),
    })

    if chrome_email and openclaw_email != chrome_email:
        last_attempt_ts = int(state.get('lastAutoLoginAttemptTs') or 0)
        if now_ts() - last_attempt_ts >= AUTO_LOGIN_COOLDOWN_SECONDS:
            print(f'chrome/openclaw mismatch detected: chrome={chrome_email} openclaw={openclaw_email}; triggering auto login')
            state['lastAutoLoginAttemptAt'] = now_iso()
            state['lastAutoLoginAttemptTs'] = now_ts()
            save_state(state)
            try:
                subprocess.run(['python3', str(AUTO_LOGIN_SCRIPT), '--email', chrome_email], cwd=str(ROOT), check=True)
                profiles = load_profiles()
                selected = choose_profile(profiles, chrome_email)
                if selected is not None:
                    profile_id, profile = selected
                    openclaw_email = str(profile.get('email') or '').strip()
                    fingerprint = compute_fingerprint(profile_id, profile)
                    state.update({
                        'selectedProfile': profile_id,
                        'selectedEmail': openclaw_email,
                        'expires': int(profile.get('expires') or 0),
                        'lastAutoLoginResult': 'success',
                    })
            except subprocess.CalledProcessError as err:
                state['lastAutoLoginResult'] = f'failed:{err.returncode}'
                state['status'] = 'auto_login_failed'
                save_state(state)
                raise
        else:
            state['status'] = 'awaiting_auto_login_cooldown'
            save_state(state)
            print(f'chrome/openclaw mismatch still present but cooling down: chrome={chrome_email} openclaw={openclaw_email}')
            return

    if previous == fingerprint:
        state['status'] = 'unchanged'
        save_state(state)
        print(f'profile unchanged: {profile_id}')
        return

    print(f'profile changed: {profile_id}')
    subprocess.run([str(SYNC_SCRIPT), '--profile-id', profile_id], cwd=str(ROOT), check=True)
    state.update({
        'fingerprint': fingerprint,
        'status': 'synced',
        'lastSyncedAt': now_iso(),
        'lastAutoLoginResult': state.get('lastAutoLoginResult') or '',
    })
    save_state(state)
    print(f'synced profile: {profile_id}')


if __name__ == '__main__':
    main()
