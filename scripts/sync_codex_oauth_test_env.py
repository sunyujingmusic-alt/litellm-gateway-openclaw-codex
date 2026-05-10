#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path('/Users/sunyujing/litellm-gateway')
AUTH_JSON = Path('/Users/sunyujing/.openclaw/agents/main/agent/auth-profiles.json')
AUTH_STATE_JSON = Path('/Users/sunyujing/.openclaw/agents/main/agent/auth-state.json')
PREFERRED_ACCOUNT_JSON = ROOT / 'tmp' / 'openai_plus_account_extracted.json'
CHROME_HELPER = ROOT / 'scripts' / 'get_chrome_chatgpt_account.js'
PROD_ENV = ROOT / '.env'
TEST_ENV = ROOT / '.env.codex-oauth-gmn.test'

ORDERED_PROD_KEYS = [
    'CCODEX_UPSTREAM_BASE_URL',
    'CCODEX_UPSTREAM_API_KEY',
    'GMN_UPSTREAM_BASE_URL',
    'GMN_UPSTREAM_API_KEY',
    'OAUTH_UPSTREAM_BASE_URL',
    'OAUTH_UPSTREAM_API_KEY',
    'OAUTH_UPSTREAM_EXPIRES',
    'OAUTH_UPSTREAM_ACCOUNT_ID',
    'OAUTH_UPSTREAM_EMAIL',
    'OAUTH_UPSTREAM_PLAN_TYPE',
    'GATEWAY_API_KEY',
    'PUBLIC_MODEL_NAME',
]

ORDERED_TEST_KEYS = [
    'CCODEX_UPSTREAM_BASE_URL',
    'CCODEX_UPSTREAM_API_KEY',
    'GMN_UPSTREAM_BASE_URL',
    'GMN_UPSTREAM_API_KEY',
    'OAUTH_UPSTREAM_BASE_URL',
    'OAUTH_UPSTREAM_API_KEY',
    'OAUTH_UPSTREAM_EXPIRES',
    'OAUTH_UPSTREAM_ACCOUNT_ID',
    'OAUTH_UPSTREAM_EMAIL',
    'OAUTH_UPSTREAM_PLAN_TYPE',
    'GATEWAY_API_KEY',
    'TEST_REDIS_URL',
]


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


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


def write_env(path: Path, content: dict[str, str], ordered_keys: list[str]) -> None:
    lines = [f'{key}={content[key]}' for key in ordered_keys if key in content]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def load_profiles() -> dict[str, dict]:
    auth = load_json(AUTH_JSON)
    profiles = auth.get('profiles') if isinstance(auth, dict) and isinstance(auth.get('profiles'), dict) else auth
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


def resolve_node_binary() -> str:
    for candidate in (shutil.which('node'), '/opt/homebrew/bin/node', '/usr/local/bin/node'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return 'node'


def get_chrome_account() -> dict:
    node_bin = resolve_node_binary()
    try:
        proc = subprocess.run([node_bin, str(CHROME_HELPER)], cwd=str(ROOT), capture_output=True, text=True)
    except Exception as exc:
        return {'ok': False, 'error': f'chrome_helper_failed:{exc}', 'nodeBinary': node_bin}
    raw = (proc.stdout or '').strip()
    if not raw:
        return {'ok': False, 'error': 'chrome_helper_empty_stdout'}
    try:
        return json.loads(raw)
    except Exception:
        return {'ok': False, 'error': 'chrome_helper_invalid_json', 'raw': raw[:500]}


def pick_auth_state_last_good(profiles: dict[str, dict]) -> tuple[str | None, str | None]:
    if AUTH_STATE_JSON.exists():
        try:
            state = load_json(AUTH_STATE_JSON)
            profile_id = str(((state.get('lastGood') or {}).get('openai-codex')) or '').strip()
            if profile_id and isinstance(profiles.get(profile_id), dict):
                return profile_id, 'auth_state_last_good'
        except Exception:
            pass
    return None, None


def pick_debug_preferred_profile_id(profiles: dict[str, dict]) -> tuple[str | None, str | None]:
    if os.environ.get('SYNC_ALLOW_PREFERRED_ACCOUNT_FILE', '').strip() not in {'1', 'true', 'TRUE', 'yes', 'YES'}:
        return None, None
    if PREFERRED_ACCOUNT_JSON.exists():
        try:
            preferred = load_json(PREFERRED_ACCOUNT_JSON)
            profile_id = str(preferred.get('profileId') or '').strip()
            if profile_id and isinstance(profiles.get(profile_id), dict):
                return profile_id, 'preferred_account_file'
        except Exception:
            pass
    return None, None


def choose_profile(
    profiles: dict[str, dict],
    explicit_profile_id: str | None = None,
    chrome_email: str = '',
    allow_nonmatching_fallback: bool = False,
) -> tuple[str, dict, str]:
    candidates = list_candidate_profiles(profiles)
    if not candidates:
        raise SystemExit('no usable openai-codex oauth profile found in auth-profiles.json')

    if explicit_profile_id:
        profile = profiles.get(explicit_profile_id)
        if not isinstance(profile, dict):
            raise SystemExit(f'missing profile: {explicit_profile_id}')
        return explicit_profile_id, profile, 'explicit'

    normalized_email = chrome_email.strip().lower()
    if normalized_email:
        email_matches = [
            (profile_id, profile)
            for profile_id, profile in candidates
            if str(profile.get('email') or '').strip().lower() == normalized_email
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
            return email_matches[0][0], email_matches[0][1], 'chrome_email_match'
        if not allow_nonmatching_fallback:
            raise SystemExit(f'no openai-codex profile matches current Chrome ChatGPT account: {chrome_email}')

    state_profile_id, state_source = pick_auth_state_last_good(profiles)
    if state_profile_id:
        profile = profiles[state_profile_id]
        if allow_nonmatching_fallback or not normalized_email or str(profile.get('email') or '').strip().lower() == normalized_email:
            return state_profile_id, profile, str(state_source)

    preferred_profile_id, preferred_source = pick_debug_preferred_profile_id(profiles)
    if preferred_profile_id:
        return preferred_profile_id, profiles[preferred_profile_id], str(preferred_source)

    def score(item: tuple[str, dict]) -> tuple[int, int, str]:
        profile_id, profile = item
        expires = int(profile.get('expires') or 0)
        has_refresh = 1 if str(profile.get('refresh') or '').strip() else 0
        return (expires, has_refresh, profile_id)

    candidates.sort(key=score, reverse=True)
    profile_id, profile = candidates[0]
    return profile_id, profile, 'latest_candidate'


def build_env_contents(profile: dict, base: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    access = str(profile.get('access') or '').strip()
    expires = str(profile.get('expires') or '').strip()
    account_id = str(profile.get('accountId') or '').strip()
    email = str(profile.get('email') or '').strip()
    plan_type = str(profile.get('chatgptPlanType') or '').strip()
    if not access:
        raise SystemExit('selected openai-codex profile has empty access token')

    ccodex_base = base.get('CCODEX_UPSTREAM_BASE_URL') or base.get('PRIMARY_UPSTREAM_BASE_URL') or 'https://cdn2.ccodex.net/v1'
    ccodex_key = base.get('CCODEX_UPSTREAM_API_KEY') or base.get('PRIMARY_UPSTREAM_API_KEY') or ''
    gmn_base = base.get('GMN_UPSTREAM_BASE_URL') or base.get('BACKUP_UPSTREAM_BASE_URL') or 'https://gmn.chuangzuoli.com/v1'
    gmn_key = base.get('GMN_UPSTREAM_API_KEY') or base.get('BACKUP_UPSTREAM_API_KEY') or ''
    gateway_key = base.get('GATEWAY_API_KEY') or 'local-litellm-gateway'
    public_model_name = base.get('PUBLIC_MODEL_NAME') or 'gpt-5.4'

    auth_base = 'https://chatgpt.com/backend-api/codex'
    common = {
        'CCODEX_UPSTREAM_BASE_URL': ccodex_base,
        'CCODEX_UPSTREAM_API_KEY': ccodex_key,
        'GMN_UPSTREAM_BASE_URL': gmn_base,
        'GMN_UPSTREAM_API_KEY': gmn_key,
        'OAUTH_UPSTREAM_BASE_URL': auth_base,
        'OAUTH_UPSTREAM_API_KEY': access,
        'OAUTH_UPSTREAM_EXPIRES': expires,
        'OAUTH_UPSTREAM_ACCOUNT_ID': account_id,
        'OAUTH_UPSTREAM_EMAIL': email,
        'OAUTH_UPSTREAM_PLAN_TYPE': plan_type,
        'GATEWAY_API_KEY': gateway_key,
    }

    prod_content = dict(common)
    prod_content['PUBLIC_MODEL_NAME'] = public_model_name

    test_content = dict(common)
    test_content['TEST_REDIS_URL'] = 'redis://host.docker.internal:6380/1'
    return prod_content, test_content


def main() -> None:
    parser = argparse.ArgumentParser(description='Sync OpenClaw Codex OAuth token from auth-profiles into LiteLLM env files, following the current Chrome ChatGPT account when possible.')
    parser.add_argument('--profile-id', help='Explicit auth profile id to use.')
    parser.add_argument('--chrome-email', help='Override current Chrome ChatGPT email used for matching.')
    parser.add_argument('--allow-nonmatching-fallback', action='store_true', help='Allow falling back to auth-state/latest candidate when Chrome current account has no matching OpenClaw profile yet.')
    parser.add_argument('--dry-run', action='store_true', help='Resolve selection but do not write env files.')
    parser.add_argument('--json', action='store_true', help='Print resolved selection as JSON.')
    args = parser.parse_args()

    profiles = load_profiles()
    chrome = get_chrome_account()
    chrome_email = str(args.chrome_email or '').strip()
    if not chrome_email and chrome.get('ok'):
        chrome_email = str(chrome.get('email') or '').strip()

    selected_profile_id, profile, selected_source = choose_profile(
        profiles,
        explicit_profile_id=args.profile_id,
        chrome_email=chrome_email,
        allow_nonmatching_fallback=args.allow_nonmatching_fallback,
    )

    access = str(profile.get('access') or '').strip()
    expires = str(profile.get('expires') or '').strip()
    email = str(profile.get('email') or '').strip()
    account_id = str(profile.get('accountId') or '').strip()
    plan_type = str(profile.get('chatgptPlanType') or '').strip()
    if not access:
        raise SystemExit(f'empty openai-codex access token in profile: {selected_profile_id}')

    base = parse_env(PROD_ENV)
    prod_content, test_content = build_env_contents(profile, base)

    result = {
        'selectedProfile': selected_profile_id,
        'selectionSource': selected_source,
        'selectedEmail': email,
        'selectedAccountId': account_id,
        'selectedPlanType': plan_type,
        'oauthExpires': expires,
        'chrome': {
            'ok': bool(chrome.get('ok')),
            'email': str(chrome.get('email') or '').strip(),
            'name': str(chrome.get('name') or '').strip(),
            'selectedProfileId': str(chrome.get('selectedProfileId') or '').strip(),
            'selectedProfileName': str(chrome.get('selectedProfileName') or '').strip(),
            'selectionSource': str(chrome.get('selectionSource') or '').strip(),
            'error': '' if chrome.get('ok') else str(chrome.get('error') or ''),
        },
    }

    if not args.dry_run:
        write_env(PROD_ENV, prod_content, ORDERED_PROD_KEYS)
        write_env(TEST_ENV, test_content, ORDERED_TEST_KEYS)
        result['wrote'] = [str(PROD_ENV), str(TEST_ENV)]

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f'selected_profile={selected_profile_id}')
        print(f'selection_source={selected_source}')
        if chrome_email:
            print(f'chrome_email={chrome_email}')
        if email:
            print(f'selected_email={email}')
        if account_id:
            print(f'selected_account_id={account_id}')
        if plan_type:
            print(f'selected_plan_type={plan_type}')
        if not args.dry_run:
            print(f'wrote {PROD_ENV}')
            print(f'wrote {TEST_ENV}')
        print(f'oauth_expires={expires}')


if __name__ == '__main__':
    main()
