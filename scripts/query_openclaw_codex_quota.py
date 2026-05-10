#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import sync_codex_oauth_test_env as sync

USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
USER_AGENT = 'codex-cli'


def window_name(seconds: int | None) -> str:
    if seconds == 18000:
        return 'five_hour'
    if seconds == 604800:
        return 'seven_day'
    if not seconds or seconds <= 0:
        return 'unknown'
    hours = seconds // 3600
    if hours >= 24:
        return f'{hours // 24}_day'
    return f'{hours}_hour'


def to_iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def pct_remaining(used_percent: float | None) -> float | None:
    if used_percent is None:
        return None
    return round(max(0.0, 100.0 - float(used_percent)), 2)


def fetch_quota(access_token: str, account_id: str = '') -> dict:
    headers = {
        'Authorization': f'Bearer {access_token}',
        'User-Agent': USER_AGENT,
        'Accept': 'application/json',
    }
    if account_id:
        headers['ChatGPT-Account-Id'] = account_id
    req = urllib.request.Request(USAGE_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = getattr(resp, 'status', 200)
            body = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        return {'ok': False, 'status': exc.code, 'error': body[:1000]}
    except Exception as exc:
        return {'ok': False, 'status': 0, 'error': str(exc)}

    try:
        payload = json.loads(body)
    except Exception as exc:
        return {'ok': False, 'status': status, 'error': f'invalid_json:{exc}', 'raw': body[:1000]}

    rate_limit = payload.get('rate_limit') or {}
    windows = []
    for key in ('primary_window', 'secondary_window'):
        item = rate_limit.get(key) or {}
        used_percent = item.get('used_percent')
        limit_window_seconds = item.get('limit_window_seconds')
        reset_at = item.get('reset_at')
        if used_percent is None:
            continue
        windows.append({
            'slot': key,
            'name': window_name(limit_window_seconds),
            'limitWindowSeconds': limit_window_seconds,
            'usedPercent': used_percent,
            'remainingPercent': pct_remaining(used_percent),
            'resetAt': reset_at,
            'resetAtIso': to_iso(reset_at),
        })

    return {
        'ok': True,
        'status': status,
        'windows': windows,
        'raw': payload,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Query ChatGPT Plus/Pro Codex quota (5h / 7d windows) from the currently selected OpenClaw Codex OAuth profile.')
    parser.add_argument('--profile-id', help='Explicit OpenClaw auth profile id to use.')
    parser.add_argument('--chrome-email', help='Override current Chrome ChatGPT email used for matching.')
    parser.add_argument('--allow-nonmatching-fallback', action='store_true')
    parser.add_argument('--json', action='store_true', help='Print full JSON (default behavior).')
    args = parser.parse_args()

    profiles = sync.load_profiles()
    chrome = sync.get_chrome_account()
    chrome_email = str(args.chrome_email or '').strip()
    if not chrome_email and chrome.get('ok'):
        chrome_email = str(chrome.get('email') or '').strip()

    profile_id, profile, selected_source = sync.choose_profile(
        profiles,
        explicit_profile_id=args.profile_id,
        chrome_email=chrome_email,
        allow_nonmatching_fallback=args.allow_nonmatching_fallback,
    )

    access = str(profile.get('access') or '').strip()
    account_id = str(profile.get('accountId') or '').strip()
    email = str(profile.get('email') or '').strip()
    plan_type = str(profile.get('chatgptPlanType') or '').strip()

    result = fetch_quota(access, account_id)
    result.update({
        'selectedProfile': profile_id,
        'selectionSource': selected_source,
        'selectedEmail': email,
        'selectedAccountId': account_id,
        'selectedPlanType': plan_type,
        'chrome': {
            'ok': bool(chrome.get('ok')),
            'email': str(chrome.get('email') or '').strip(),
            'name': str(chrome.get('name') or '').strip(),
            'selectedProfileId': str(chrome.get('selectedProfileId') or '').strip(),
            'selectedProfileName': str(chrome.get('selectedProfileName') or '').strip(),
            'selectionSource': str(chrome.get('selectionSource') or '').strip(),
            'error': '' if chrome.get('ok') else str(chrome.get('error') or ''),
        },
        'queriedAt': datetime.now(timezone.utc).isoformat(),
    })

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
