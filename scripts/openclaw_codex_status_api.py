#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / 'scripts'
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import sync_codex_oauth_test_env as sync  # noqa: E402
import query_openclaw_codex_quota as quota_mod  # noqa: E402

def env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


ENV_PATH = env_path('LITELLM_ENV_PATH', ROOT / '.env')
WATCHER_STATE_PATH = ROOT / 'scripts' / '.watch_openclaw_codex_profile_state.json'
LITELLM_CONFIG_PATH = env_path('LITELLM_CONFIG_PATH', ROOT / 'litellm' / 'config.yaml')
LITELLM_COMPOSE_PATH = env_path('LITELLM_COMPOSE_PATH', ROOT / 'docker-compose.yml')
LITELLM_HEALTH_URL = os.environ.get('LITELLM_HEALTH_URL', 'http://192.168.199.102:4002/health/liveliness')
FAILOVER_STATS_URL = os.environ.get('FAILOVER_STATS_URL', 'http://127.0.0.1:4129/failover-stats')
REDIS_CONTAINER = os.environ.get('LITELLM_REDIS_CONTAINER', 'litellm-router-redis')
LITELLM_RESTART_CONTAINER = os.environ.get('LITELLM_RESTART_CONTAINER', 'litellm-router-prod')
DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = env_int('LITELLM_PANEL_PORT', 4010)
DOCKER_BIN = shutil.which('docker') or '/opt/homebrew/bin/docker'

DEFAULT_PRODUCTION_CHAINS = [
    {
        'id': 'gpt-5.6-sol',
        'label': 'OpenClaw gpt-5.6-sol',
        'owner': 'OpenClaw',
        'entryModel': 'gpt-5.6-sol',
        'statusKey': 'gateway:health:gpt-5.6-sol',
        'cooldownKey': 'deployment:gpt-5.6-sol:cooldown',
    },
    {
        'id': 'gpt-5.5',
        'label': 'OpenClaw gpt-5.5',
        'owner': 'OpenClaw',
        'entryModel': 'gpt-5.5',
        'statusKey': 'gateway:health:gpt-5.5',
        'cooldownKey': 'deployment:gpt-5.5:cooldown',
    },
    {
        'id': 'gpt-5.4',
        'label': 'OpenClaw gpt-5.4',
        'owner': 'OpenClaw',
        'entryModel': 'gpt-5.4',
        'statusKey': 'gateway:health:gpt-5.4',
        'cooldownKey': 'deployment:gpt-5.4:cooldown',
    },
]


def normalize_chain_config(item: dict[str, Any]) -> dict[str, str] | None:
    entry_model = str(item.get('entryModel') or item.get('id') or '').strip()
    if not entry_model:
        return None
    chain_id = str(item.get('id') or entry_model).strip()
    return {
        'id': chain_id,
        'label': str(item.get('label') or entry_model).strip(),
        'owner': str(item.get('owner') or 'Gateway').strip(),
        'entryModel': entry_model,
        'statusKey': str(item.get('statusKey') or f'gateway:health:{entry_model}').strip(),
        'cooldownKey': str(item.get('cooldownKey') or f'deployment:{entry_model}:cooldown').strip(),
    }


def load_production_chains() -> list[dict[str, str]]:
    raw = os.environ.get('LITELLM_PANEL_CHAINS', '').strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            print(f'warning: invalid LITELLM_PANEL_CHAINS: {exc}', file=sys.stderr)
        else:
            if isinstance(parsed, list):
                chains = [
                    chain
                    for item in parsed
                    if isinstance(item, dict)
                    for chain in [normalize_chain_config(item)]
                    if chain
                ]
                if chains:
                    return chains
    return [
        chain
        for item in DEFAULT_PRODUCTION_CHAINS
        for chain in [normalize_chain_config(item)]
        if chain
    ]


def choose_default_entry_model(chains: list[dict[str, str]]) -> str:
    requested = os.environ.get('LITELLM_DEFAULT_ENTRY_MODEL', '').strip()
    if requested:
        return requested
    for chain in chains:
        if str(chain.get('entryModel') or '').strip() == 'gpt-5.4':
            return 'gpt-5.4'
    return str((chains[0] if chains else {}).get('entryModel') or 'gpt-5.4')


PRODUCTION_CHAINS = load_production_chains()
DEFAULT_ENTRY_MODEL = choose_default_entry_model(PRODUCTION_CHAINS)
ENTRY_MODEL = DEFAULT_ENTRY_MODEL

EDITABLE_ENTRY_MODELS = tuple(str(chain.get('entryModel') or '') for chain in PRODUCTION_CHAINS)

UI_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LiteLLM Gateway WebUI</title>
  <style>
    :root {
      --bg: #eef2f6;
      --paper: #ffffff;
      --ink: #18212f;
      --muted: #5f6b7a;
      --line: #d7dee8;
      --accent: #0f766e;
      --accent-2: #2563eb;
      --accent-3: #0f172a;
      --ok: #16794f;
      --warn: #a35d00;
      --bad: #b42318;
      --pending: #7c5d12;
      --info: #1d4ed8;
      --shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
      --shadow-soft: 0 8px 24px rgba(15, 23, 42, 0.06);
      --radius: 12px;
      --mono: "SFMono-Regular", "Menlo", "Monaco", monospace;
      --sans: "Avenir Next", "PingFang SC", "Noto Sans SC", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: var(--sans);
      color: var(--ink);
      background: var(--bg);
      min-height: 100vh;
    }
    .shell {
      width: min(1480px, calc(100vw - 32px));
      margin: 24px auto 48px;
    }
    .hero {
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: calc(var(--radius) + 6px);
      box-shadow: var(--shadow);
      padding: 28px;
      position: relative;
      overflow: hidden;
    }
    .hero:before { display: none; }
    h1 {
      margin: 0 0 8px;
      font-size: clamp(28px, 4vw, 44px);
      line-height: 1.02;
      letter-spacing: 0;
    }
    .sub {
      margin: 0;
      max-width: 760px;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.6;
    }
    .status-row {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 22px;
    }
    .chip {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      padding: 14px 16px;
    }
    .chip .label {
      display: block;
      font-size: 11px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }
    .chip .value {
      font-size: 18px;
      font-weight: 700;
      line-height: 1.2;
      word-break: break-word;
    }
    .failover-summary-row {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
    }
    .failover-summary-stack {
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }
    .failover-summary-row .chip {
      padding: 12px 14px;
    }
    .failover-summary-row .chip .label {
      letter-spacing: 0;
      text-transform: none;
    }
    .failover-summary-row .chip .value {
      font-size: 26px;
      line-height: 1;
    }
    .failover-tools {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
      margin-top: 16px;
    }
    .failover-range {
      display: inline-flex;
      gap: 6px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
    }
    .range-btn, .failover-reset-btn {
      border: 0;
      border-radius: 999px;
      cursor: pointer;
      padding: 8px 12px;
      font-family: inherit;
      font-weight: 800;
      font-size: 13px;
      transition: transform 140ms ease, opacity 140ms ease, background 140ms ease;
    }
    .range-btn {
      background: transparent;
      color: var(--muted);
    }
    .range-btn.is-active {
      background: var(--accent);
      color: white;
      box-shadow: 0 6px 16px rgba(15,118,110,0.18);
    }
    .failover-reset-btn {
      background: rgba(180,83,9,0.11);
      color: var(--warn);
    }
    .range-btn:hover, .failover-reset-btn:hover { transform: translateY(-1px); }
    .range-btn:disabled, .failover-reset-btn:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
    .chip.ok .value { color: var(--ok); }
    .chip.warn .value { color: var(--warn); }
    .chip.bad .value { color: var(--bad); }
    .hero-meta {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border-radius: 999px;
      background: #f8fafc;
      border: 1px solid var(--line);
      font-size: 13px;
      color: var(--muted);
    }
    .pill strong {
      color: var(--accent-3);
      font-weight: 800;
    }
    .grid {
      display: grid;
      grid-template-columns: 1.5fr 0.95fr;
      gap: 18px;
      margin-top: 18px;
      align-items: start;
    }
    .chain-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
      margin-top: 18px;
      align-items: start;
    }
    .chain-column {
      display: grid;
      gap: 18px;
      min-width: 0;
    }
    .panel {
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 20px;
      min-width: 0;
    }
    .panel-title-row {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 10px;
    }
    .panel-title-row h2 {
      margin-bottom: 4px;
    }
    .chain-kicker {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-family: var(--mono);
      line-height: 1.3;
    }
    .panel h2 {
      margin: 0 0 10px;
      font-size: 20px;
      letter-spacing: -0.03em;
    }
    .panel p.meta {
      margin: 0 0 18px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
    }
    .stack {
      display: grid;
      gap: 12px;
    }
    .card {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      padding: 14px;
      display: grid;
      gap: 12px;
      position: relative;
      box-shadow: var(--shadow-soft);
      transition: transform 140ms ease, box-shadow 140ms ease, border-color 140ms ease;
    }
    .card:hover {
      transform: translateY(-1px);
      box-shadow: 0 14px 28px rgba(61, 49, 33, 0.12);
    }
    .card.is-main {
      border-color: rgba(15,118,110,0.45);
      background: #f4fbf8;
    }
    .card.is-pending {
      outline: 2px dashed rgba(197,107,44,0.35);
      outline-offset: 3px;
    }
    .card.is-dragging {
      opacity: 0.45;
      transform: scale(0.98);
    }
    .card.drag-over-top::before,
    .card.drag-over-bottom::after {
      content: "";
      position: absolute;
      left: 16px;
      right: 16px;
      height: 4px;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(15,118,110,0.2), rgba(15,118,110,0.92), rgba(15,118,110,0.2));
      box-shadow: 0 0 0 2px rgba(255,250,242,0.8);
    }
    .card.drag-over-top::before {
      top: -3px;
    }
    .card.drag-over-bottom::after {
      bottom: -3px;
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }
    .card-info {
      min-width: 0;
    }
    .slot {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      background: rgba(15,118,110,0.09);
      color: var(--accent);
    }
    .slot.fallback {
      background: rgba(197,107,44,0.1);
      color: var(--accent-2);
    }
    .model-name {
      margin: 0;
      font-size: 20px;
      font-weight: 800;
      letter-spacing: -0.03em;
    }
    .model-sub {
      margin: 4px 0 0;
      font-size: 13px;
      color: var(--muted);
      font-family: var(--mono);
      word-break: break-word;
    }
    .route-light {
      position: relative;
      gap: 7px;
      transition: background 160ms ease, border-color 160ms ease, color 160ms ease, box-shadow 160ms ease;
    }
    .route-light::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: rgba(111,123,116,0.45);
      box-shadow: none;
    }
    .route-light.is-live {
      background: rgba(47,133,90,0.16);
      border-color: rgba(47,133,90,0.38);
      color: var(--ok);
      box-shadow: 0 0 0 3px rgba(47,133,90,0.08), 0 0 18px rgba(47,133,90,0.22);
    }
    .route-light.is-live::before {
      background: var(--ok);
      box-shadow: 0 0 0 3px rgba(47,133,90,0.14), 0 0 12px rgba(47,133,90,0.72);
    }
    .station-stats {
      width: min(210px, 32%);
      min-width: 170px;
      align-self: flex-end;
      border: 1px solid rgba(176, 146, 94, 0.22);
      border-radius: 10px;
      background: #f8fafc;
      padding: 12px;
      display: grid;
      gap: 8px;
    }
    .station-stats .stat-label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .station-stats .stat-main {
      font-size: 28px;
      line-height: 1;
      font-weight: 860;
      color: var(--accent-3);
    }
    .station-stat-line {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .station-stat-line strong {
      color: var(--ink);
      font-size: 13px;
    }
    .card-bottom {
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      gap: 14px;
    }
    .controls {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .card-topline {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
    }
    .drag-handle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      user-select: none;
      cursor: grab;
      padding: 8px 10px;
      border-radius: 999px;
      background: rgba(31,45,42,0.06);
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-weight: 700;
    }
    .drag-handle:active {
      cursor: grabbing;
    }
    .controls button, .toolbar button, .panel-actions button, .pending-actions button {
      border: 0;
      border-radius: 999px;
      cursor: pointer;
      padding: 10px 14px;
      font-family: inherit;
      font-weight: 700;
      font-size: 14px;
      transition: transform 140ms ease, opacity 140ms ease, background 140ms ease;
    }
    .controls button:hover, .toolbar button:hover, .panel-actions button:hover, .pending-actions button:hover { transform: translateY(-1px); }
    .controls button:disabled, .toolbar button:disabled, .panel-actions button:disabled, .pending-actions button:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
    .btn-main { background: var(--accent); color: white; }
    .btn-soft { background: rgba(15,118,110,0.12); color: var(--accent); }
    .btn-ghost { background: rgba(31,45,42,0.08); color: var(--ink); }
    .btn-danger-soft { background: rgba(180,83,9,0.11); color: var(--warn); }
    .toolbar {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 18px;
    }
    .save-hint {
      font-size: 13px;
      color: var(--muted);
      padding-left: 2px;
    }
    .msg {
      min-height: 24px;
      margin-top: 12px;
      font-size: 14px;
      font-weight: 600;
    }
    .msg.ok { color: var(--ok); }
    .msg.warn { color: var(--warn); }
    .msg.bad { color: var(--bad); }
    .msg.info { color: var(--info); }
    .pool {
      display: grid;
      gap: 10px;
    }
    .pool-item {
      border: 1px dashed var(--line);
      border-radius: 10px;
      padding: 12px 14px;
      display: grid;
      gap: 6px;
      background: #fff;
      transition: transform 140ms ease, box-shadow 140ms ease, border-color 140ms ease;
    }
    .pool-item:hover {
      transform: translateY(-1px);
      box-shadow: var(--shadow-soft);
    }
    .pool-item.disabled {
      opacity: 0.78;
      background: #f1f5f9;
    }
    .pool-title {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      font-weight: 700;
    }
    .tag {
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      background: rgba(31,45,42,0.08);
      color: var(--muted);
    }
    .tag.is-clickable {
      cursor: pointer;
      background: rgba(15,118,110,0.12);
      color: var(--accent);
    }
    .tag.is-active {
      background: rgba(15,118,110,0.15);
      color: var(--accent);
    }
    .tag.is-bypassed {
      background: rgba(180,83,9,0.12);
      color: var(--warn);
    }
    .code {
      font-family: var(--mono);
      font-size: 12px;
      color: var(--muted);
      word-break: break-all;
    }
    .helper {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--accent-3);
      font-size: 11px;
      font-weight: 800;
      cursor: help;
    }
    [data-tooltip] {
      position: relative;
    }
    [data-tooltip]::after {
      content: attr(data-tooltip);
      position: absolute;
      left: 50%;
      bottom: calc(100% + 10px);
      transform: translateX(-50%) translateY(6px);
      background: rgba(24, 33, 31, 0.96);
      color: #fffaf2;
      font-size: 12px;
      line-height: 1.45;
      padding: 9px 11px;
      border-radius: 10px;
      width: max-content;
      max-width: 260px;
      box-shadow: 0 12px 28px rgba(15, 18, 17, 0.28);
      opacity: 0;
      pointer-events: none;
      transition: opacity 120ms ease, transform 120ms ease;
      z-index: 30;
      white-space: normal;
    }
    [data-tooltip]::before {
      content: "";
      position: absolute;
      left: 50%;
      bottom: calc(100% + 4px);
      transform: translateX(-50%) translateY(6px);
      border: 6px solid transparent;
      border-top-color: rgba(24, 33, 31, 0.96);
      opacity: 0;
      transition: opacity 120ms ease, transform 120ms ease;
      pointer-events: none;
      z-index: 30;
    }
    [data-tooltip]:hover::after,
    [data-tooltip]:hover::before,
    [data-tooltip]:focus-visible::after,
    [data-tooltip]:focus-visible::before {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }
    .pending-bar {
      display: none;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 16px;
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(163,93,0,0.1);
      border: 1px solid rgba(163,93,0,0.22);
      color: var(--pending);
      font-size: 14px;
      font-weight: 700;
    }
    .pending-bar.is-visible {
      display: flex;
    }
    .pending-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .panel-actions {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }
    .note {
      margin-top: 16px;
      border-top: 1px solid var(--line);
      padding-top: 14px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }
    .overlay {
      position: fixed;
      inset: 0;
      background: rgba(22, 24, 23, 0.36);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
      z-index: 20;
    }
    .overlay.open { display: flex; }
    .modal {
      width: min(680px, 100%);
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 28px 60px rgba(22, 24, 23, 0.26);
      padding: 22px;
    }
    .modal-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 16px;
    }
    .modal-head h3 {
      margin: 0;
      font-size: 22px;
      letter-spacing: -0.03em;
    }
    .form-grid {
      display: grid;
      gap: 14px;
    }
    .field label {
      display: block;
      margin-bottom: 6px;
      font-size: 13px;
      font-weight: 700;
      color: var(--muted);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .field input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      font: inherit;
      color: var(--ink);
      background: rgba(255,255,255,0.86);
    }
    .field small {
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .modal-actions {
      margin-top: 18px;
      display: flex;
      gap: 10px;
      justify-content: flex-end;
      flex-wrap: wrap;
    }
    .runtime-alerts {
      display: grid;
      gap: 8px;
      margin-top: 16px;
    }
    .runtime-alert {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: #f8fafc;
      padding: 12px 14px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .runtime-alert strong {
      color: var(--ink);
      display: block;
      margin-bottom: 3px;
    }
    .runtime-alert.is-warn {
      background: #fff7ed;
      border-color: #fed7aa;
      color: #9a3412;
    }
    .runtime-alert.is-bad {
      background: #fef2f2;
      border-color: #fecaca;
      color: #991b1b;
    }
    .runtime-board {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 16px;
    }
    .runtime-chain {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      padding: 14px;
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .runtime-chain.is-cooldown,
    .runtime-chain.is-unhealthy {
      border-color: #fecaca;
      background: #fffafa;
    }
    .runtime-chain.is-recovering {
      border-color: #fed7aa;
      background: #fffbf5;
    }
    .runtime-chain-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
    }
    .runtime-title {
      display: grid;
      gap: 3px;
      min-width: 0;
    }
    .runtime-title strong {
      font-size: 16px;
      line-height: 1.2;
    }
    .runtime-title span {
      color: var(--muted);
      font-size: 12px;
      font-family: var(--mono);
      word-break: break-word;
    }
    .state-badge {
      border-radius: 999px;
      padding: 6px 9px;
      font-size: 11px;
      font-weight: 800;
      white-space: nowrap;
      background: #e2e8f0;
      color: #334155;
      text-transform: uppercase;
    }
    .state-badge.is-ok {
      background: #dcfce7;
      color: #166534;
    }
    .state-badge.is-warn {
      background: #ffedd5;
      color: #9a3412;
    }
    .state-badge.is-bad {
      background: #fee2e2;
      color: #991b1b;
    }
    .runtime-metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .runtime-metric {
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      background: #f8fafc;
      padding: 9px 10px;
      min-width: 0;
    }
    .runtime-metric span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.2;
      margin-bottom: 5px;
    }
    .runtime-metric strong {
      display: block;
      font-size: 15px;
      line-height: 1.2;
      word-break: break-word;
    }
    .runtime-detail {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      word-break: break-word;
      max-height: 3.2em;
      overflow: hidden;
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-top: 18px;
      margin-bottom: 10px;
    }
    .section-head h2 {
      margin: 0;
      font-size: 20px;
      letter-spacing: 0;
    }
    .section-head p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .last-updated {
      align-self: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 10px;
      color: var(--muted);
      background: #fff;
      font-size: 12px;
      white-space: nowrap;
    }
    @media (max-width: 900px) {
      .status-row, .failover-summary-row, .runtime-board, .runtime-metrics, .grid, .chain-grid { grid-template-columns: 1fr; }
      .failover-tools { align-items: stretch; flex-direction: column; }
      .failover-range { width: 100%; }
      .range-btn { flex: 1; }
      .card-head { flex-direction: column; }
      .card-bottom { flex-direction: column; align-items: stretch; }
      .station-stats { width: 100%; min-width: 0; }
      .section-head { flex-direction: column; }
      .last-updated { align-self: flex-start; }
      .panel-title-row { flex-direction: column; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <h1>LiteLLM Gateway WebUI</h1>
      <p class="sub">网关状态、独立请求链路的 cooldown / probe，以及各模型族独立的路由顺序编辑都在这里。</p>
      <div class="hero-meta">
        <div class="pill"><strong>操作范围</strong> 按模型族独立保存</div>
        <div class="pill"><strong>保存动作</strong> 写回配置并重启 LiteLLM</div>
        <div class="pill"><strong>运行告警</strong> <span id="runtime-summary-pill">loading...</span></div>
      </div>
      <div class="status-row">
        <div class="chip">
          <span class="label">LiteLLM Process</span>
          <span class="value" id="gateway-health">loading...</span>
        </div>
        <div class="chip">
          <span class="label">Chrome / OpenClaw</span>
          <span class="value" id="binding-email">loading...</span>
        </div>
        <div class="chip">
          <span class="label">Active Primaries</span>
          <span class="value" id="current-main">loading...</span>
        </div>
        <div class="chip">
          <span class="label" id="current-fallbacks-label">Active Fallbacks</span>
          <span class="value" id="current-fallbacks">loading...</span>
        </div>
      </div>
      <div class="runtime-alerts" id="runtime-alerts"></div>
      <div class="failover-tools">
        <div class="failover-range" aria-label="故障转移统计范围">
          <button type="button" class="range-btn is-active" data-failover-window="today" data-tooltip="查看今天 0 点以来的故障转移统计。">今日</button>
          <button type="button" class="range-btn" data-failover-window="3d" data-tooltip="查看最近 3 天内的故障转移统计。">3 天内</button>
          <button type="button" class="range-btn" data-failover-window="7d" data-tooltip="查看最近 7 天内的故障转移统计。">7 天内</button>
        </div>
        <button type="button" class="failover-reset-btn" id="failover-reset-btn" data-tooltip="把当前统计显示从此刻重新计数。原始 SQLite 和 JSONL 日志会保留。">清除统计</button>
      </div>
      <div class="failover-summary-stack" id="failover-summary-stack"></div>
    </section>
    <section class="panel">
      <div class="section-head">
        <div>
          <h2>Gateway Chains</h2>
          <p>双链路只读总览，展示当前 probe、cooldown 和统计范围内的请求分布。</p>
        </div>
        <div class="last-updated" id="runtime-last-updated">loading...</div>
      </div>
      <div class="runtime-board" id="runtime-board"></div>
    </section>
    <section class="chain-grid" id="chain-grid" aria-label="模型路由编辑"></section>
  </main>
  <div id="edit-overlay" class="overlay" aria-hidden="true">
    <div class="modal">
      <div class="modal-head">
        <h3 id="edit-title">编辑中转站</h3>
        <button class="btn-ghost" id="edit-close-btn" data-tooltip="关闭当前编辑窗口，不会保存本次输入。">关闭</button>
      </div>
      <div class="form-grid">
        <div class="field">
          <label for="edit-model-name">名称</label>
          <input id="edit-model-name" type="text" autocomplete="off">
          <small>修改后会同步更新模型池和当前调用链中的显示名称。</small>
        </div>
        <div class="field">
          <label for="edit-base-url">请求地址</label>
          <input id="edit-base-url" type="text" autocomplete="off">
          <small id="edit-base-url-key"></small>
        </div>
        <div class="field">
          <label for="edit-api-key">API Key</label>
          <input id="edit-api-key" type="text" autocomplete="off">
          <small id="edit-api-key-note"></small>
        </div>
      </div>
      <div class="modal-actions">
        <button class="btn-ghost" id="edit-cancel-btn" data-tooltip="放弃本次修改并关闭窗口。">取消</button>
        <button class="btn-danger-soft" id="edit-delete-btn" data-tooltip="仅允许删除当前处于 bypassed 的中转站。删除后会移除该模型定义及其环境变量，并自动重启 LiteLLM。" hidden>删除中转站</button>
        <button class="btn-soft" id="edit-test-btn" data-tooltip="使用当前输入的名称、请求地址和 API key 做一次真实连通性测试，不会保存配置。">测试当前中转站</button>
        <button class="btn-main" id="edit-save-btn" data-tooltip="立即写入这个中转站的新参数，并重启 LiteLLM。这个操作会立刻生效，不依赖主界面的保存按钮。">保存并重启 LiteLLM</button>
      </div>
    </div>
  </div>
  <div id="new-model-overlay" class="overlay" aria-hidden="true">
    <div class="modal">
      <div class="modal-head">
        <h3 id="new-model-title">添加新模型</h3>
        <button class="btn-ghost" id="new-model-close-btn" data-tooltip="关闭当前新建窗口，不会创建模型。">关闭</button>
      </div>
      <div class="form-grid">
        <div class="field">
          <label for="new-model-name">名称</label>
          <input id="new-model-name" type="text" autocomplete="off">
          <small>建议使用清晰的业务别名，方便在主模型和备用模型顺序中识别。</small>
        </div>
        <div class="field">
          <label for="new-model-base-url">请求地址</label>
          <input id="new-model-base-url" type="text" autocomplete="off">
        </div>
        <div class="field">
          <label for="new-model-api-key">API Key</label>
          <input id="new-model-api-key" type="text" autocomplete="off">
        </div>
      </div>
      <div class="modal-actions">
        <button class="btn-ghost" id="new-model-cancel-btn" data-tooltip="放弃本次创建并关闭窗口。">取消</button>
        <button class="btn-main" id="new-model-save-btn" data-tooltip="创建新的中转站模型，写入配置与环境变量，并自动重启 LiteLLM。">创建并重启 LiteLLM</button>
      </div>
    </div>
  </div>
  <script>
    const state = {
      status: null,
      chains: [],
      editingModel: null,
      newModelEntryModel: null,
      dirtyByEntryModel: {},
      drag: null,
      failoverStats: null,
      failoverWindow: 'today',
      focusedEntryModel: 'gpt-5.4',
    };

    function entryKey(entryModel) {
      return String(entryModel || '').replace(/[^a-zA-Z0-9]+/g, '-').replace(/^-|-$/g, '');
    }

    function chainRoot(entryModel) {
      return document.querySelector('[data-entry-model="' + entryModel + '"]');
    }

    function setFocusedEntryModel(entryModel) {
      if (entryModel) {
        state.focusedEntryModel = entryModel;
      }
    }

    function setMessage(entryModel, text, kind = '') {
      const root = chainRoot(entryModel);
      const node = root?.querySelector('[data-message]');
      if (!node) return;
      node.textContent = text || '';
      node.className = 'msg' + (kind ? ' ' + kind : '');
    }

    function setDirty(entryModel, value) {
      state.dirtyByEntryModel[entryModel] = !!value;
    }

    function badgeHealth(ok) {
      return ok ? 'healthy' : 'unhealthy';
    }

    function preferredChains(chains) {
      const priority = new Map([['gpt-5.6-sol', 0], ['gpt-5.5', 1], ['gpt-5.4', 2]]);
      return (chains || []).slice().sort((a, b) => {
        const aRank = priority.has(a.entryModel) ? priority.get(a.entryModel) : 99;
        const bRank = priority.has(b.entryModel) ? priority.get(b.entryModel) : 99;
        if (aRank !== bRank) return aRank - bRank;
        return String(a.entryModel || '').localeCompare(String(b.entryModel || ''));
      });
    }

    function getOrder(entryModel) {
      return state.chains.find(order => order.entryModel === entryModel) || null;
    }

    function activeModels(entryModel) {
      return (getOrder(entryModel)?.models || []).filter(item => item.enabled);
    }

    function inactiveModels(entryModel) {
      return (getOrder(entryModel)?.models || []).filter(item => !item.enabled);
    }

    function orderLabel(order) {
      return String(order?.label || order?.entryModel || 'Model family');
    }

    function ownerLabel(order) {
      return String(order?.owner || 'OpenClaw');
    }

    function renderHeader() {
      const summary = state.status?.summary || {};
      const primaryText = state.chains.map(order => {
        const active = activeModels(order.entryModel);
        return order.entryModel + ': ' + (active[0]?.model_name || '-');
      }).join(' | ');
      const fallbackText = state.chains.map(order => {
        const active = activeModels(order.entryModel);
        const names = active.slice(1).map(item => item.model_name).join(' > ') || 'none';
        return order.entryModel + ': ' + names;
      }).join(' | ');
      document.getElementById('gateway-health').textContent = badgeHealth(!!summary.litellmHealthy);
      document.getElementById('binding-email').textContent = summary.envBoundEmail || summary.resolvedProfileEmail || '-';
      document.getElementById('current-main').textContent = primaryText || '-';
      document.getElementById('current-fallbacks').textContent = fallbackText || '-';
      document.getElementById('current-fallbacks-label').textContent = 'Active Fallbacks';
    }

    function failoverGroupForEntryModel(entryModel) {
      const normalizedEntryModel = normalizeModelName(entryModel);
      const groups = state.failoverStats?.chainGroups || [];
      return groups.find(group => normalizeModelName(group.primary || group.models?.[0]) === normalizedEntryModel)
        || groups.find(group => (group.models || []).some(model => normalizeModelName(model) === normalizedEntryModel))
        || null;
    }

    function runtimeLabel(chain) {
      return String(chain?.label || chain?.entryModel || 'Gateway chain');
    }

    function renderRuntime() {
      const root = document.getElementById('runtime-board');
      const runtime = state.status?.runtime || {};
      const chains = Array.isArray(runtime.chains) ? runtime.chains : [];
      const generatedAt = runtime.generatedAt || state.status?.generatedAt || '';
      document.getElementById('runtime-last-updated').textContent = generatedAt
        ? '状态时间: ' + new Date(generatedAt).toLocaleString('zh-CN')
        : '状态时间: unavailable';

      const alerts = [];
      root.innerHTML = '';
      if (!chains.length) {
        root.innerHTML = '<div class="runtime-chain"><strong>运行时状态不可用</strong><p>无法读取 Redis cooldown 与 probe 状态。</p></div>';
        document.getElementById('runtime-alerts').innerHTML = '';
        document.getElementById('runtime-summary-pill').textContent = 'runtime unavailable';
        return;
      }

      chains.forEach((chain) => {
        const label = runtimeLabel(chain);
        const group = failoverGroupForEntryModel(chain.entryModel);
        const summary = group?.summary || {};
        const probe = chain.probe || {};
        const latest = group?.recentEvents?.find(event => event.success && Number.isFinite(Number(event.depth))) || null;
        const latestModel = latest?.model || latest?.deployment || '-';
        const probeState = String(probe.state || '').toLowerCase();
        const probeStatus = probe.available
          ? (probeState || (probe.ok ? 'healthy' : 'unknown'))
          : 'missing';
        const cooldown = chain.cooldown || {};
        const cooldownActive = !!cooldown.active;
        const cooldownSeconds = Number(cooldown.ttlSeconds || 0);
        const cooldownStatus = cooldownActive ? (cooldownSeconds > 0 ? cooldownSeconds + 's' : 'active') : 'clear';
        const badgeClass = (probeStatus === 'healthy' || probeStatus === 'idle')
          ? 'is-ok'
          : (probeStatus === 'probing' || probeStatus === 'missing' || probeStatus === 'unavailable' || cooldownActive)
            ? 'is-warn'
            : (probeStatus === 'unhealthy' || probeStatus === 'malformed' ? 'is-bad' : '');
        const runtimeDetail = cooldownActive
          ? (probeStatus === 'probing'
            ? `该入口模型正在 cooldown，探针已连续成功 ${formatCount(probe.consecutiveSuccesses)} 次。`
            : '该入口模型正在 cooldown，新的请求会绕过它并按本链路 fallback 规则继续。')
          : (probe.available
            ? (probeStatus === 'idle'
              ? '未处于 cooldown，探针待命。'
              : (probe.ok ? '最近健康探针成功。' : ('最近健康探针失败：' + (probe.detail || probe.error || 'unknown error'))))
            : '尚未记录该入口模型的健康探针。');
        const card = document.createElement('article');
        card.className = 'runtime-chain'
          + (cooldownActive ? ' is-cooldown' : '')
          + (probeStatus === 'probing' ? ' is-recovering' : '')
          + (probeStatus === 'unhealthy' ? ' is-unhealthy' : '');
        card.innerHTML = `
          <div class="runtime-chain-head">
            <div class="runtime-title">
              <strong>${escapeHtml(label)}</strong>
              <span>${escapeHtml((chain.owner || 'Gateway') + ' · ' + (chain.entryModel || '-'))}</span>
            </div>
            <span class="state-badge ${badgeClass}">${escapeHtml(probeStatus)}</span>
          </div>
          <div class="runtime-metrics">
            <div class="runtime-metric"><span>当前入口</span><strong>${escapeHtml(chain.entryModel || '-')}</strong></div>
            <div class="runtime-metric"><span>Cooldown</span><strong>${escapeHtml(cooldownStatus)}</strong></div>
            <div class="runtime-metric"><span>最近实际落点</span><strong>${escapeHtml(latestModel)}</strong></div>
            <div class="runtime-metric"><span>统计范围请求</span><strong>${formatCount(summary.totalRequests)}</strong></div>
            <div class="runtime-metric"><span>进入备用</span><strong>${formatCount(summary.backupRequests)}</strong></div>
            <div class="runtime-metric"><span>未恢复失败</span><strong>${formatCount(summary.unresolvedFailures)}</strong></div>
          </div>
          <div class="runtime-detail">${escapeHtml(runtimeDetail)}</div>
        `;
        root.appendChild(card);

        if (cooldownActive) {
          alerts.push(`${label} cooldown ${cooldownSeconds > 0 ? cooldownSeconds + 's' : 'active'}`);
        }
        if (probeStatus === 'unhealthy' || probeStatus === 'malformed' || probeStatus === 'unavailable') {
          alerts.push(`${label} probe unhealthy`);
        }
      });

      document.getElementById('runtime-summary-pill').textContent = alerts.length ? alerts.join(' | ') : '两条链路无 cooldown / probe 告警';
      document.getElementById('runtime-alerts').innerHTML = alerts.length
        ? alerts.map(alert => '<span class="runtime-alert">' + escapeHtml(alert) + '</span>').join('')
        : '';
    }

    function renderFailoverSummary() {
      const root = document.getElementById('failover-summary-stack');
      const groupsByEntryModel = new Map((state.failoverStats?.chainGroups || [])
        .map(group => [normalizeModelName(group.primary || group.models?.[0]), group]));
      const rows = state.chains.length
        ? state.chains.map((chain) => {
            const group = groupsByEntryModel.get(normalizeModelName(chain.entryModel)) || failoverGroupForEntryModel(chain.entryModel);
            return {
              entryModel: chain.entryModel,
              label: chain.label || group?.label || ('OpenClaw ' + chain.entryModel),
              summary: group?.summary || {},
            };
          })
        : (state.failoverStats?.chainGroups || []).map((group) => ({
            entryModel: group.primary || group.models?.[0] || '',
            label: group.label || group.primary || 'OpenClaw',
            summary: group.summary || {},
          }));

      if (!rows.length) {
        rows.push({
          entryModel: state.focusedEntryModel || 'gpt-5.4',
          label: 'OpenClaw ' + (state.focusedEntryModel || 'gpt-5.4'),
          summary: activeFailoverStats()?.summary || {},
        });
      }

      root.innerHTML = rows.map((row) => {
        const summary = row.summary || {};
        return `
          <div class="failover-summary-row" data-summary-entry-model="${escapeHtml(row.entryModel)}">
            <div class="chip">
              <span class="label">${escapeHtml(row.label)} 总请求</span>
              <span class="value">${formatCount(summary.totalRequests)}</span>
            </div>
            <div class="chip ok">
              <span class="label">${escapeHtml(row.label)} 主链完成</span>
              <span class="value">${formatCount(summary.primaryCompletions)}</span>
            </div>
            <div class="chip warn">
              <span class="label">${escapeHtml(row.label)} 进入备用</span>
              <span class="value">${formatCount(summary.backupRequests)}</span>
            </div>
            <div class="chip warn">
              <span class="label">${escapeHtml(row.label)} 第二备用</span>
              <span class="value">${formatCount(summary.depth2OrMore)}</span>
            </div>
            <div class="chip bad">
              <span class="label">${escapeHtml(row.label)} 未恢复失败</span>
              <span class="value">${formatCount(summary.unresolvedFailures)}</span>
            </div>
          </div>
        `;
      }).join('');
      document.querySelectorAll('[data-failover-window]').forEach(button => {
        button.classList.toggle('is-active', button.dataset.failoverWindow === state.failoverWindow);
      });
    }

    function formatCount(value) {
      const num = Number(value || 0);
      return Number.isFinite(num) ? num.toLocaleString('zh-CN') : '0';
    }

    function normalizeModelName(value) {
      return String(value || '').trim().toLowerCase();
    }

    function selectedFailoverGroup() {
      return failoverGroupForEntryModel(state.focusedEntryModel || 'gpt-5.4');
    }

    function activeFailoverStats(entryModel = state.focusedEntryModel) {
      return failoverGroupForEntryModel(entryModel) || state.failoverStats || {};
    }

    function stationStatsFor(entryModel, item, index) {
      const chain = activeFailoverStats(entryModel)?.chain || [];
      const byDepth = chain.find(row => Number(row.depth) === index);
      if (byDepth) return byDepth;
      const modelName = normalizeModelName(item.model_name);
      return chain.find(row => normalizeModelName(row.model) === modelName) || {};
    }

    function renderStationStats(row, index) {
      const called = Number(row.called || 0);
      const success = index === 0
        ? Number(row.finalSuccesses || 0)
        : Number(row.fallbackSuccesses || row.finalSuccesses || 0);
      const failure = Number(row.finalFailures || 0) + Number(row.fallbackFailures || 0);
      const successLabel = index === 0 ? '主链完成' : '承接成功';
      return `
        <div class="station-stats" data-tooltip="当前统计范围内，这个中转站在调用链中被实际走到的次数。">
          <span class="stat-label">被调用次数</span>
          <strong class="stat-main">${formatCount(called)}</strong>
          <div class="station-stat-line"><span>${successLabel}</span><strong>${formatCount(success)}</strong></div>
          <div class="station-stat-line"><span>失败记录</span><strong>${formatCount(failure)}</strong></div>
        </div>
      `;
    }

    function swap(arr, from, to) {
      const copy = arr.slice();
      const [item] = copy.splice(from, 1);
      copy.splice(to, 0, item);
      return copy;
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function latestRouteDepthFor(entryModel) {
      const recent = activeFailoverStats(entryModel)?.recentEvents || [];
      const relevantTypes = new Set([
        'client_request_success',
        'client_request_failure',
        'request_success',
        'request_failure',
        'fallback_success',
        'fallback_failure',
      ]);
      const latest = recent.find(event => relevantTypes.has(event.eventType));
      if (!latest || !latest.success) return null;
      const depth = Number(latest.depth || 0);
      return Number.isFinite(depth) ? depth : null;
    }

    function clearDragState() {
      state.drag = null;
    }

    function syncOrder(entryModel, active, inactive) {
      const order = getOrder(entryModel);
      if (!order) return;
      const nextActiveNames = new Set(active.map(item => item.model_name));
      order.models = active.map(item => ({ ...item, enabled: true }))
        .concat(
          inactive
            .filter(item => !nextActiveNames.has(item.model_name))
            .map(item => ({ ...item, enabled: false }))
        );
    }

    function createUnsavedSnapshot() {
      const chains = {};
      state.chains.forEach(order => {
        chains[order.entryModel] = {
          activeUpstreamIds: activeModels(order.entryModel).map(item => item.upstreamId),
          inactiveUpstreamIds: inactiveModels(order.entryModel).map(item => item.upstreamId),
        };
      });
      return {
        dirtyByEntryModel: { ...state.dirtyByEntryModel },
        chains,
      };
    }

    function applyUnsavedSnapshotToOrder(order, snapshot) {
      const entryModel = order?.entryModel;
      const dirty = !!snapshot?.dirtyByEntryModel?.[entryModel];
      const saved = snapshot?.chains?.[entryModel];
      if (!dirty || !saved || !order?.models) {
        return order;
      }
      const byUpstreamId = new Map((order.models || []).map(item => [item.upstreamId, item]));
      const nextActive = [];
      const seen = new Set();
      (saved.activeUpstreamIds || []).forEach((upstreamId) => {
        const item = byUpstreamId.get(upstreamId);
        if (!item || seen.has(upstreamId)) {
          return;
        }
        seen.add(upstreamId);
        nextActive.push({ ...item, enabled: true });
      });
      (order.models || []).forEach((item) => {
        if (item.enabled && !seen.has(item.upstreamId)) {
          seen.add(item.upstreamId);
          nextActive.push({ ...item, enabled: true });
        }
      });
      const nextInactive = [];
      (saved.inactiveUpstreamIds || []).forEach((upstreamId) => {
        const item = byUpstreamId.get(upstreamId);
        if (!item || seen.has(upstreamId)) {
          return;
        }
        seen.add(upstreamId);
        nextInactive.push({ ...item, enabled: false });
      });
      (order.models || []).forEach((item) => {
        if (!seen.has(item.upstreamId)) {
          seen.add(item.upstreamId);
          nextInactive.push({ ...item, enabled: false });
        }
      });
      return {
        ...order,
        models: nextActive.concat(nextInactive),
      };
    }

    function reorderActiveModel(entryModel, dragModelName, targetModelName, position) {
      if (!dragModelName || !targetModelName || !position || dragModelName === targetModelName) {
        return false;
      }
      const active = activeModels(entryModel);
      const inactive = inactiveModels(entryModel);
      const fromIndex = active.findIndex(item => item.model_name === dragModelName);
      const targetIndex = active.findIndex(item => item.model_name === targetModelName);
      if (fromIndex < 0 || targetIndex < 0) {
        return false;
      }
      const next = active.slice();
      const [moved] = next.splice(fromIndex, 1);
      let insertIndex = targetIndex;
      if (fromIndex < targetIndex) {
        insertIndex -= 1;
      }
      if (position === 'bottom') {
        insertIndex += 1;
      }
      insertIndex = Math.max(0, Math.min(insertIndex, next.length));
      next.splice(insertIndex, 0, moved);
      if (next.every((item, index) => item.model_name === active[index]?.model_name)) {
        return false;
      }
      syncOrder(entryModel, next, inactive);
      setDirty(entryModel, true);
      setFocusedEntryModel(entryModel);
      rerender();
      setMessage(entryModel, '顺序已通过拖拽更新，尚未保存。', 'warn');
      return true;
    }

    function renderActive(entryModel, root) {
      const active = activeModels(entryModel);
      const dirty = !!state.dirtyByEntryModel[entryModel];
      root.innerHTML = '';
      if (!active.length) {
        root.innerHTML = '<div class="pool-item disabled">当前没有启用中的模型。</div>';
        return;
      }
      active.forEach((item, index) => {
        const card = document.createElement('article');
        const row = stationStatsFor(entryModel, item, index);
        const liveDepth = latestRouteDepthFor(entryModel);
        const isLive = liveDepth === index;
        const lightTooltip = isLive
          ? '最近一次成功请求由这个中转站承接。'
          : '最近一次成功请求没有落到这个中转站。';
        const dragOverClass = state.drag?.entryModel === entryModel && state.drag?.overModelName === item.model_name && state.drag?.overPosition
          ? ' drag-over-' + state.drag.overPosition
          : '';
        card.className = 'card'
          + (index === 0 ? ' is-main' : '')
          + (dirty ? ' is-pending' : '')
          + (state.drag?.entryModel === entryModel && state.drag?.modelName === item.model_name ? ' is-dragging' : '')
          + dragOverClass;
        card.draggable = true;
        card.dataset.index = String(index);
        card.dataset.modelName = item.model_name;
        const slot = index === 0 ? 'Main' : 'Fallback #' + index;
        card.innerHTML = `
          <div class="card-topline">
            <span class="drag-handle" data-tooltip="按住后拖拽这张卡片，可直接调整主模型和备用模型的真实顺序。">drag to reorder</span>
            <span class="tag route-light ${isLive ? 'is-live' : ''}" data-tooltip="${lightTooltip}">${escapeHtml(item.upstreamKey || 'UNKNOWN')}</span>
          </div>
          <div class="card-head">
            <div class="card-info">
              <span class="slot ${index === 0 ? '' : 'fallback'}">${slot}</span>
              <p class="model-name">${escapeHtml(item.model_name)}</p>
              <p class="model-sub">${escapeHtml(item.apiBase || '-')} </p>
            </div>
          </div>
          <div class="card-bottom">
            <div class="controls">
              <button class="btn-main" data-action="main" data-index="${index}" data-tooltip="把该模型提升为主模型，当前第一位的主模型会变成 fallback #1。">设为主模型</button>
              <button class="btn-danger-soft" data-action="disable" data-index="${index}" data-tooltip="把该模型切换为 bypassed。参数会保留，但当前不会被调用。">bypass</button>
              <button class="btn-ghost" data-action="edit" data-index="${index}" data-tooltip="修改这个中转站的名称、请求地址和 API key。保存后会立即重启 LiteLLM。">edit</button>
            </div>
            ${renderStationStats(row, index)}
          </div>
        `;
        root.appendChild(card);
        card.addEventListener('dragstart', (event) => {
          setFocusedEntryModel(entryModel);
          state.drag = {
            entryModel,
            modelName: item.model_name,
            overModelName: null,
            overPosition: null,
          };
          card.classList.add('is-dragging');
          if (event.dataTransfer) {
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', item.model_name);
          }
        });
        card.addEventListener('dragover', (event) => {
          if (!state.drag || state.drag.entryModel !== entryModel || state.drag.modelName === item.model_name) {
            return;
          }
          event.preventDefault();
          const rect = card.getBoundingClientRect();
          const midpoint = rect.top + rect.height / 2;
          state.drag.overModelName = item.model_name;
          state.drag.overPosition = event.clientY < midpoint ? 'top' : 'bottom';
          card.classList.toggle('drag-over-top', state.drag.overPosition === 'top');
          card.classList.toggle('drag-over-bottom', state.drag.overPosition === 'bottom');
        });
        card.addEventListener('dragleave', (event) => {
          if (!card.contains(event.relatedTarget)) {
            if (state.drag?.overModelName === item.model_name) {
              state.drag.overModelName = null;
              state.drag.overPosition = null;
            }
            card.classList.remove('drag-over-top', 'drag-over-bottom');
          }
        });
        card.addEventListener('drop', (event) => {
          event.preventDefault();
          const dragModelName = state.drag?.modelName || event.dataTransfer?.getData('text/plain') || '';
          const position = state.drag?.overPosition || 'bottom';
          const changed = reorderActiveModel(entryModel, dragModelName, item.model_name, position);
          clearDragState();
          if (!changed) {
            rerender();
          }
        });
        card.addEventListener('dragend', () => {
          clearDragState();
          rerender();
        });
      });
      root.querySelectorAll('button').forEach(button => {
        const index = Number(button.dataset.index);
        const action = button.dataset.action;
        if (action === 'disable' && active.length === 1) button.disabled = true;
        button.addEventListener('click', () => mutateActive(entryModel, action, index));
      });
    }

    function renderPool(entryModel, root) {
      const activeSet = new Set(activeModels(entryModel).map(item => item.model_name));
      const items = [...activeModels(entryModel), ...inactiveModels(entryModel)];
      root.innerHTML = '';
      items.forEach(item => {
        const active = activeSet.has(item.model_name);
        const node = document.createElement('div');
        node.className = 'pool-item' + (active ? '' : ' disabled');
        node.innerHTML = `
          <div class="pool-title">
            <span>${item.model_name}</span>
            <span class="tag is-clickable ${active ? 'is-active' : 'is-bypassed'}" data-action="toggle-status" data-name="${item.model_name}" data-tooltip="${active ? '点击后切换为 bypassed。该模型将从当前调用链移除，但参数仍保留。' : '点击后切换为 active。该模型会回到当前调用链队尾，随后可再调整顺序。'}">${active ? 'active' : 'bypassed'}</span>
          </div>
          <div class="code">${item.apiBase || '-'}</div>
          <div class="controls">
            <button class="btn-main" data-action="enable-main" data-name="${item.model_name}" data-tooltip="直接把这个模型设为主模型，并自动变为 active。">设为主模型</button>
            <button class="btn-soft" data-action="enable-fallback" data-name="${item.model_name}" data-tooltip="把这个模型加入 active 队列尾部，作为备用模型。">加入备用队列</button>
            <button class="btn-ghost" data-action="edit" data-name="${item.model_name}" data-tooltip="修改这个模型对应中转站的名称、请求地址和 API key。">edit</button>
          </div>
        `;
        root.appendChild(node);
      });
      root.querySelectorAll('[data-action]').forEach(node => {
        const name = node.dataset.name;
        const action = node.dataset.action;
        if (node.tagName === 'BUTTON' && action === 'enable-fallback' && activeSet.has(name)) node.disabled = true;
        node.addEventListener('click', () => {
          setFocusedEntryModel(entryModel);
          if (action === 'edit') {
            openEdit(entryModel, name);
            return;
          }
          if (action === 'toggle-status') {
            togglePoolStatus(entryModel, name);
            return;
          }
          mutatePool(entryModel, action, name);
        });
      });
    }

    function rerender() {
      renderHeader();
      renderRuntime();
      renderFailoverSummary();
      renderChains();
    }

    function mutateActive(entryModel, action, index) {
      setFocusedEntryModel(entryModel);
      const active = activeModels(entryModel);
      const inactive = inactiveModels(entryModel);
      let next = active.slice();
      if (action === 'main' && index > 0) next = swap(next, index, 0);
      if (action === 'edit') {
        openEdit(entryModel, active[index].model_name);
        return;
      }
      if (action === 'disable') {
        const [removed] = next.splice(index, 1);
        inactive.push({ ...removed, enabled: false });
      }
      syncOrder(entryModel, next, inactive);
      setDirty(entryModel, true);
      rerender();
      setMessage(entryModel, '顺序已更新，尚未保存。', 'warn');
    }

    function mutatePool(entryModel, action, modelName) {
      const models = getOrder(entryModel)?.models || [];
      const found = models.find(item => item.model_name === modelName);
      if (!found) return;
      let active = activeModels(entryModel);
      const inactive = inactiveModels(entryModel).filter(item => item.model_name !== modelName);
      if (action === 'enable-main') {
        active = active.filter(item => item.model_name !== modelName);
        active.unshift({ ...found, enabled: true });
      }
      if (action === 'enable-fallback' && !active.find(item => item.model_name === modelName)) {
        active.push({ ...found, enabled: true });
      }
      if (action === 'disable') {
        active = active.filter(item => item.model_name !== modelName);
      }
      syncOrder(entryModel, active, inactive);
      setDirty(entryModel, true);
      rerender();
      setMessage(entryModel, '顺序已更新，尚未保存。', 'warn');
    }

    function togglePoolStatus(entryModel, modelName) {
      const active = activeModels(entryModel);
      const inActive = active.find(item => item.model_name === modelName);
      if (inActive) {
        if (active.length === 1) {
          setMessage(entryModel, '至少需要保留一个 active 模型。', 'bad');
          return;
        }
        mutatePool(entryModel, 'disable', modelName);
        return;
      }
      mutatePool(entryModel, 'enable-fallback', modelName);
    }

    async function fetchJson(url, options = {}) {
      const res = await fetch(url, options);
      const payload = await res.json();
      if (!res.ok) {
        throw new Error(payload.error || payload.message || ('HTTP ' + res.status));
      }
      return payload;
    }

    function failoverStatsUrl() {
      return '/failover-stats?window=' + encodeURIComponent(state.failoverWindow);
    }

    async function loadFailoverStats() {
      state.failoverStats = await fetchJson(failoverStatsUrl()).catch(() => null);
      renderFailoverSummary();
      renderChains();
    }

    async function setFailoverWindow(windowName) {
      state.failoverWindow = windowName;
      await loadFailoverStats();
      const label = { today: '今日', '3d': '3 天内', '7d': '7 天内' }[windowName] || windowName;
      setMessage(state.focusedEntryModel, '已切换统计范围：' + label, 'ok');
    }

    async function loadAll() {
      const [status, order, failoverStats] = await Promise.all([
        fetchJson('/status'),
        fetchJson('/router-config'),
        fetchJson(failoverStatsUrl()).catch(() => null)
      ]);
      state.status = status;
      state.chains = preferredChains(Array.isArray(order.chains) && order.chains.length ? order.chains : [order]);
      state.failoverStats = failoverStats;
      state.dirtyByEntryModel = {};
      state.chains.forEach(chain => setDirty(chain.entryModel, false));
      if (!getOrder(state.focusedEntryModel) && state.chains[0]) {
        state.focusedEntryModel = state.chains[0].entryModel;
      }
      rerender();
    }

    async function refreshAfterImmediateModelChange(entryModel, messageText) {
      const snapshot = createUnsavedSnapshot();
      const [status, freshOrder, failoverStats] = await Promise.all([
        fetchJson('/status'),
        fetchJson('/router-config'),
        fetchJson(failoverStatsUrl()).catch(() => null),
      ]);
      state.status = status;
      state.failoverStats = failoverStats;
      state.chains = preferredChains((Array.isArray(freshOrder.chains) && freshOrder.chains.length ? freshOrder.chains : [freshOrder])
        .map(order => applyUnsavedSnapshotToOrder(order, snapshot)));
      state.dirtyByEntryModel = { ...snapshot.dirtyByEntryModel };
      rerender();
      setMessage(entryModel, messageText, 'ok');
    }

    async function resetFailoverStats() {
      const ok = window.confirm('确认清除当前统计显示并从此刻重新计数？原始 SQLite/JSONL 日志会保留。');
      if (!ok) return;
      const button = document.getElementById('failover-reset-btn');
      button.disabled = true;
      try {
        await fetchJson('/failover-stats/reset', { method: 'POST' });
        await loadFailoverStats();
        setMessage(state.focusedEntryModel, '统计已清零，后续请求会从当前时间重新累计。', 'ok');
      } catch (err) {
        setMessage(state.focusedEntryModel, '清除统计失败：' + err.message, 'bad');
      } finally {
        button.disabled = false;
      }
    }

    async function save(entryModel) {
      setFocusedEntryModel(entryModel);
      const active = activeModels(entryModel);
      if (!active.length) {
        setMessage(entryModel, '至少需要保留一个启用中的模型。', 'bad');
        return;
      }
      const payload = {
        entryModel,
        entryUpstreamId: active[0].upstreamId,
        fallbackChain: active.slice(1).map(item => item.upstreamId),
      };
      setMessage(entryModel, '正在写回配置并重启 LiteLLM...', 'warn');
      chainRoot(entryModel)?.querySelectorAll('[data-action="save"]').forEach(node => { node.disabled = true; });
      try {
        const result = await fetchJson('/router-config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        await loadAll();
        setMessage(entryModel, '保存成功，LiteLLM 已重启。' + (result.message ? ' ' + result.message : ''), 'ok');
      } catch (err) {
        setMessage(entryModel, '保存失败：' + err.message, 'bad');
      } finally {
        chainRoot(entryModel)?.querySelectorAll('[data-action="save"]').forEach(node => { node.disabled = false; });
      }
    }

    function closeEdit() {
      state.editingModel = null;
      const overlay = document.getElementById('edit-overlay');
      overlay.classList.remove('open');
      overlay.setAttribute('aria-hidden', 'true');
    }

    function openEdit(entryModel, modelName) {
      const model = (getOrder(entryModel)?.models || []).find(item => item.model_name === modelName);
      if (!model) return;
      setFocusedEntryModel(entryModel);
      state.editingModel = { entryModel, model };
      const deleteButton = document.getElementById('edit-delete-btn');
      document.getElementById('edit-title').textContent = '编辑中转站: ' + model.model_name + ' (' + entryModel + ')';
      document.getElementById('edit-model-name').value = model.model_name || '';
      document.getElementById('edit-base-url').value = model.baseUrlValue || '';
      document.getElementById('edit-api-key').value = '';
      document.getElementById('edit-base-url-key').textContent = model.baseUrlEnvKey ? ('ENV: ' + model.baseUrlEnvKey) : '';
      document.getElementById('edit-api-key-note').textContent = model.apiKeyEnvKey ? ('当前已配置: ' + (model.apiKeyMasked || 'empty') + ' | ENV: ' + model.apiKeyEnvKey) : '';
      deleteButton.hidden = !!model.enabled;
      deleteButton.disabled = false;
      const overlay = document.getElementById('edit-overlay');
      overlay.classList.add('open');
      overlay.setAttribute('aria-hidden', 'false');
    }

    async function saveEdit() {
      if (!state.editingModel) return;
      const entryModel = state.editingModel.entryModel;
      const editing = state.editingModel.model;
      const modelName = document.getElementById('edit-model-name').value.trim();
      const baseUrl = document.getElementById('edit-base-url').value.trim();
      const apiKeyInput = document.getElementById('edit-api-key').value;
      const apiKey = apiKeyInput.trim();
      if (!modelName || !baseUrl || !apiKey) {
        setMessage(entryModel, '名称、请求地址、API key 都不能为空。', 'bad');
        return;
      }
      const button = document.getElementById('edit-save-btn');
      button.disabled = true;
      setMessage(entryModel, '正在更新中转站参数并重启 LiteLLM...', 'warn');
      try {
        await fetchJson('/router-config/model', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            entryModel,
            upstreamId: editing.upstreamId,
            modelName,
            baseUrl,
            apiKey,
          }),
        });
        closeEdit();
        await refreshAfterImmediateModelChange(entryModel, '中转站参数已更新，LiteLLM 已重启。');
      } catch (err) {
        setMessage(entryModel, '中转站参数更新失败：' + err.message, 'bad');
      } finally {
        button.disabled = false;
      }
    }

    async function testEditConnection() {
      if (!state.editingModel) return;
      const entryModel = state.editingModel.entryModel;
      const editing = state.editingModel.model;
      const modelName = document.getElementById('edit-model-name').value.trim();
      const baseUrl = document.getElementById('edit-base-url').value.trim();
      const apiKeyInput = document.getElementById('edit-api-key').value;
      const apiKey = apiKeyInput.trim();
      if (!modelName || !baseUrl || !apiKey) {
        setMessage(entryModel, '测试前需要填入名称、请求地址和 API key。', 'bad');
        return;
      }
      const button = document.getElementById('edit-test-btn');
      button.disabled = true;
      setMessage(entryModel, '正在测试中转站连通性...', 'info');
      try {
        const result = await fetchJson('/router-config/model/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            entryModel,
            upstreamId: editing.upstreamId,
            modelName,
            baseUrl,
            apiKey,
          }),
        });
        const detail = result.probe?.detail ? (' ' + result.probe.detail) : '';
        setMessage(entryModel, '测试成功：' + (result.probe?.method || 'probe_ok') + detail, 'ok');
      } catch (err) {
        setMessage(entryModel, '测试失败：' + err.message, 'bad');
      } finally {
        button.disabled = false;
      }
    }

    async function deleteEditModel() {
      if (!state.editingModel) return;
      const entryModel = state.editingModel.entryModel;
      const editing = state.editingModel.model;
      if (editing.enabled) {
        setMessage(entryModel, '当前处于 active 的中转站不能删除。请先切到 bypassed。', 'bad');
        return;
      }
      const modelName = editing.model_name || '';
      const confirmed = window.confirm('确认删除中转站 "' + modelName + '" 吗？该操作会移除模型定义和对应环境变量，并重启 LiteLLM。');
      if (!confirmed) {
        return;
      }
      const button = document.getElementById('edit-delete-btn');
      button.disabled = true;
      setMessage(entryModel, '正在删除中转站并重启 LiteLLM...', 'warn');
      try {
        await fetchJson('/router-config/model/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            entryModel,
            upstreamId: editing.upstreamId,
          }),
        });
        closeEdit();
        await refreshAfterImmediateModelChange(entryModel, '中转站已删除，LiteLLM 已重启。');
      } catch (err) {
        setMessage(entryModel, '删除中转站失败：' + err.message, 'bad');
      } finally {
        button.disabled = false;
      }
    }

    function closeNewModel() {
      const overlay = document.getElementById('new-model-overlay');
      overlay.classList.remove('open');
      overlay.setAttribute('aria-hidden', 'true');
    }

    function openNewModel(entryModel) {
      setFocusedEntryModel(entryModel);
      state.newModelEntryModel = entryModel;
      document.getElementById('new-model-name').value = '';
      document.getElementById('new-model-base-url').value = '';
      document.getElementById('new-model-api-key').value = '';
      document.getElementById('new-model-title').textContent = '添加新模型: ' + entryModel;
      const overlay = document.getElementById('new-model-overlay');
      overlay.classList.add('open');
      overlay.setAttribute('aria-hidden', 'false');
    }

    async function saveNewModel() {
      const modelName = document.getElementById('new-model-name').value.trim();
      const entryModel = state.newModelEntryModel || state.focusedEntryModel || 'gpt-5.4';
      const baseUrl = document.getElementById('new-model-base-url').value.trim();
      const apiKey = document.getElementById('new-model-api-key').value.trim();
      if (!modelName || !baseUrl || !apiKey) {
        setMessage(entryModel, '新模型的名称、请求地址、API key 都不能为空。', 'bad');
        return;
      }
      const button = document.getElementById('new-model-save-btn');
      button.disabled = true;
      setMessage(entryModel, '正在创建新模型并重启 LiteLLM...', 'warn');
      try {
        await fetchJson('/router-config/model/new', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            entryModel,
            modelName,
            baseUrl,
            apiKey,
          }),
        });
        closeNewModel();
        await refreshAfterImmediateModelChange(entryModel, '新模型已创建并重启 LiteLLM。');
      } catch (err) {
        setMessage(entryModel, '创建新模型失败：' + err.message, 'bad');
      } finally {
        button.disabled = false;
      }
    }

    function renderChains() {
      const root = document.getElementById('chain-grid');
      root.innerHTML = '';
      state.chains.forEach(order => {
        const entryModel = order.entryModel;
        const key = entryKey(entryModel);
        const dirty = !!state.dirtyByEntryModel[entryModel];
        const column = document.createElement('section');
        column.className = 'chain-column';
        column.dataset.entryModel = entryModel;
        column.innerHTML = `
          <div class="panel" data-active-panel>
            <div class="panel-title-row">
              <div>
                <h2>${escapeHtml(ownerLabel(order))} Active Order</h2>
                <span class="chain-kicker">${escapeHtml(entryModel)} · ${escapeHtml(order.backendModel || 'missing backend')}</span>
              </div>
              <span class="tag ${dirty ? 'is-bypassed' : 'is-active'}">${dirty ? 'unsaved' : 'synced'}</span>
            </div>
            <p class="meta">顶部卡片代表当前会被实际调用的顺序。第一张卡片是主模型，后续依次是备用模型。<span class="helper" tabindex="0" data-tooltip="这里的变更先保存在页面状态里，不会立刻写入当前配置。只有点击本列的保存按钮后，才会更新该模型族的入口和 fallback。">?</span></p>
            <div id="active-stack-${key}" class="stack" data-active-stack></div>
            <div class="toolbar">
              <button class="btn-main" data-action="save" data-tooltip="只保存 ${escapeHtml(entryModel)} 这一列的顺序与 active/bypassed 状态，并重启 LiteLLM。">保存并重启 LiteLLM</button>
              <button class="btn-ghost" data-action="reload" data-tooltip="放弃页面里尚未保存的改动，重新从当前配置读取状态。">重新读取当前配置</button>
            </div>
            <div class="save-hint">拖动顺序前先决定哪些模型处于 active 状态；bypassed 模型不会进入当前调用链。</div>
            <div class="pending-bar ${dirty ? 'is-visible' : ''}" data-pending-bar>
              <span>当前有未保存的顺序或状态改动</span>
              <div class="pending-actions">
                <button class="btn-main" data-action="save">立即保存</button>
                <button class="btn-ghost" data-action="reload">放弃改动</button>
              </div>
            </div>
            <div class="msg" data-message></div>
          </div>
          <aside class="panel" data-pool-panel>
            <div class="panel-title-row">
              <div>
                <h2>${escapeHtml(ownerLabel(order))} Model Pool</h2>
                <span class="chain-kicker">${escapeHtml(orderLabel(order))}</span>
              </div>
              <span class="tag" data-tooltip="active / bypassed 切换只改变本列待保存状态。">pool</span>
            </div>
            <p class="meta">这里列出该模型族已经定义的所有上游别名。可以先切换 active / bypassed，再决定是否设为主模型或加入备用队列。</p>
            <div class="panel-actions">
              <button class="btn-main" data-action="add-model" data-tooltip="创建新的 ${escapeHtml(entryModel)} 中转站模型，写入配置并重启 LiteLLM。">添加新模型</button>
              <span class="tag">active / bypassed 可先切换, 再统一保存</span>
            </div>
            <div id="pool-${key}" class="pool" data-pool></div>
            <div class="note">
              此面板只修改 <code>${escapeHtml(entryModel)}</code> 的入口、fallback 和同族模型定义。保存后由本地服务自动重启当前 LiteLLM 容器。
            </div>
          </aside>
        `;
        root.appendChild(column);
        column.addEventListener('click', () => setFocusedEntryModel(entryModel));
        renderActive(entryModel, column.querySelector('[data-active-stack]'));
        renderPool(entryModel, column.querySelector('[data-pool]'));
        column.querySelectorAll('[data-action="save"]').forEach(button => {
          button.addEventListener('click', () => save(entryModel));
        });
        column.querySelectorAll('[data-action="reload"]').forEach(button => {
          button.addEventListener('click', loadAll);
        });
        column.querySelectorAll('[data-action="add-model"]').forEach(button => {
          button.addEventListener('click', () => openNewModel(entryModel));
        });
      });
    }

    document.querySelectorAll('[data-failover-window]').forEach(button => {
      button.addEventListener('click', () => setFailoverWindow(button.dataset.failoverWindow));
    });
    document.getElementById('failover-reset-btn').addEventListener('click', resetFailoverStats);
    document.getElementById('edit-close-btn').addEventListener('click', closeEdit);
    document.getElementById('edit-cancel-btn').addEventListener('click', closeEdit);
    document.getElementById('edit-delete-btn').addEventListener('click', deleteEditModel);
    document.getElementById('edit-test-btn').addEventListener('click', testEditConnection);
    document.getElementById('edit-save-btn').addEventListener('click', saveEdit);
    document.getElementById('new-model-close-btn').addEventListener('click', closeNewModel);
    document.getElementById('new-model-cancel-btn').addEventListener('click', closeNewModel);
    document.getElementById('new-model-save-btn').addEventListener('click', saveNewModel);
    document.getElementById('edit-overlay').addEventListener('click', (event) => {
      if (event.target.id === 'edit-overlay') closeEdit();
    });
    document.getElementById('new-model-overlay').addEventListener('click', (event) => {
      if (event.target.id === 'new-model-overlay') closeNewModel();
    });
    loadAll().catch(err => {
      document.getElementById('chain-grid').innerHTML = '<div class="panel"><h2>加载失败</h2><p class="meta">' + escapeHtml(err.message) + '</p></div>';
    });
  </script>
</body>
</html>
"""


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


def update_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    remaining = dict(updates)
    next_lines: list[str] = []
    for line in lines:
        if not line or line.lstrip().startswith('#') or '=' not in line:
            next_lines.append(line)
            continue
        key, _value = line.split('=', 1)
        key = key.strip()
        if key in remaining:
            next_lines.append(f'{key}={remaining.pop(key)}')
        else:
            next_lines.append(line)
    for key, value in remaining.items():
        next_lines.append(f'{key}={value}')
    content = '\n'.join(next_lines).rstrip() + '\n'
    path.write_text(content, encoding='utf-8')


def remove_env_keys(path: Path, keys_to_remove: list[str]) -> None:
    keys = {key.strip() for key in keys_to_remove if key and key.strip()}
    if not path.exists() or not keys:
        return
    next_lines: list[str] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:
            next_lines.append(line)
            continue
        key, _value = line.split('=', 1)
        if key.strip() in keys:
            continue
        next_lines.append(line)
    content = '\n'.join(next_lines).rstrip()
    path.write_text((content + '\n') if content else '', encoding='utf-8')


def slugify_upstream_id(value: str) -> str:
    lowered = value.strip().lower()
    chars = []
    for ch in lowered:
        if ch.isalnum():
            chars.append(ch)
        elif ch in {'-', '_', ' '}:
            chars.append('-')
    slug = ''.join(chars).strip('-')
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug or 'custom'


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding='utf-8',
    )


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


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 8) -> dict[str, Any]:
    body = json.dumps(payload).encode('utf-8')
    merged_headers = {'Content-Type': 'application/json'}
    if headers:
        merged_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=merged_headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', 200)
            raw = resp.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw
        return {'ok': True, 'status': status, 'body': parsed}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(raw) if raw else raw
        except Exception:
            parsed = raw
        return {'ok': False, 'status': exc.code, 'error': parsed}
    except Exception as exc:
        return {'ok': False, 'status': 0, 'error': str(exc)}


def proxy_failover_stats(path: str) -> dict[str, Any]:
    query = ''
    if '?' in path:
        query = path.split('?', 1)[1]
    url = FAILOVER_STATS_URL
    if query:
        url = f'{url}?{query}'
    result = fetch_json(url, timeout=5)
    if result.get('ok') and isinstance(result.get('body'), dict):
        return result.get('body')  # type: ignore[return-value]
    return {
        'ok': False,
        'generatedAt': now_iso(),
        'sourceUrl': url,
        'error': result.get('error') or 'failover_stats_unavailable',
        'status': result.get('status', 0),
        'summary': {
            'totalRequests': 0,
            'primaryCompletions': 0,
            'backupRequests': 0,
            'depth2OrMore': 0,
            'unresolvedFailures': 0,
        },
        'chainGroups': [],
    }


def reset_failover_stats() -> dict[str, Any]:
    base_url = FAILOVER_STATS_URL.rsplit('/failover-stats', 1)[0]
    url = f'{base_url}/admin/reset'
    result = post_json(url, {}, timeout=5)
    if result.get('ok') and isinstance(result.get('body'), dict):
        return result.get('body')  # type: ignore[return-value]
    return {
        'ok': False,
        'generatedAt': now_iso(),
        'sourceUrl': url,
        'error': result.get('error') or 'failover_stats_reset_unavailable',
        'status': result.get('status', 0),
    }


def redis_cli(*args: str, timeout: float = 3.0) -> dict[str, Any]:
    command = [DOCKER_BIN, 'exec', REDIS_CONTAINER, 'redis-cli', '--raw', *args]
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return {'ok': False, 'error': f'docker_not_found:{exc}'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'redis_cli_timeout:{timeout}s'}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or '').strip()
        return {'ok': False, 'error': error or f'redis_cli_failed:{completed.returncode}'}
    return {'ok': True, 'stdout': completed.stdout}


def parse_redis_json(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def redis_cooldown_status(key: str) -> dict[str, Any]:
    ttl_result = redis_cli('TTL', key)
    if not ttl_result.get('ok'):
        return {
            'available': False,
            'key': key,
            'active': False,
            'ttlSeconds': 0,
            'error': ttl_result.get('error') or 'redis_ttl_failed',
        }
    lines = [line for line in str(ttl_result.get('stdout') or '').strip().splitlines() if line.strip()]
    if not lines:
        return {
            'available': False,
            'key': key,
            'active': False,
            'ttlSeconds': 0,
            'error': 'invalid_ttl:',
        }
    try:
        ttl = int(lines[-1])
    except Exception:
        return {
            'available': False,
            'key': key,
            'active': False,
            'ttlSeconds': 0,
            'error': f'invalid_ttl:{lines[-1]}',
        }

    active = ttl != -2
    status: dict[str, Any] = {
        'available': True,
        'key': key,
        'active': active,
        'ttlSeconds': ttl if ttl > 0 else 0,
        'persistent': ttl == -1,
        'rawTtl': ttl,
    }
    if not active:
        return status

    value_result = redis_cli('GET', key)
    if value_result.get('ok'):
        raw = str(value_result.get('stdout') or '').strip()
        parsed = parse_redis_json(raw)
        if parsed:
            status['detail'] = str(parsed.get('exception_received') or parsed.get('detail') or '')[:300]
            status['statusCode'] = str(parsed.get('status_code') or '')
            status['cooldownTime'] = parsed.get('cooldown_time')
        elif raw:
            status['detail'] = raw[:300]
    else:
        status['readError'] = value_result.get('error') or 'redis_get_failed'
    return status


def redis_probe_status(key: str) -> dict[str, Any]:
    result = redis_cli('GET', key)
    if not result.get('ok'):
        return {
            'available': False,
            'key': key,
            'ok': False,
            'state': 'unavailable',
            'error': result.get('error') or 'redis_get_failed',
        }
    raw = str(result.get('stdout') or '').strip()
    if not raw:
        return {
            'available': False,
            'key': key,
            'ok': False,
            'state': 'missing',
        }
    parsed = parse_redis_json(raw)
    if not parsed:
        return {
            'available': True,
            'key': key,
            'ok': False,
            'state': 'malformed',
            'detail': raw[:300],
        }
    state = str(parsed.get('state') or '').strip().lower()
    ok = bool(parsed.get('ok'))
    return {
        'available': True,
        'key': key,
        'ok': ok,
        'state': state or ('healthy' if ok else 'unknown'),
        'detail': str(parsed.get('detail') or '')[:500],
        'checkedAt': str(parsed.get('checkedAt') or ''),
        'deploymentId': str(parsed.get('deploymentId') or ''),
        'cooldownKey': str(parsed.get('cooldownKey') or ''),
        'consecutiveSuccesses': safe_int(parsed.get('consecutiveSuccesses') or 0, default=0),
    }


def runtime_chain_state(cooldown: dict[str, Any], probe: dict[str, Any]) -> str:
    probe_state = str(probe.get('state') or '').lower()
    if cooldown.get('active'):
        if probe_state == 'probing':
            return 'recovering'
        if probe_state == 'unhealthy':
            return 'unhealthy'
        return 'cooldown'
    if probe_state in {'healthy', 'idle', 'unhealthy', 'missing', 'malformed', 'unavailable'}:
        return probe_state
    return 'unknown'


def build_runtime_status() -> dict[str, Any]:
    generated_at = now_iso()
    ping = redis_cli('PING')
    redis_ok = bool(ping.get('ok') and str(ping.get('stdout') or '').strip() == 'PONG')
    redis_info = {
        'ok': redis_ok,
        'container': REDIS_CONTAINER,
    }
    if not redis_ok:
        redis_info['error'] = ping.get('error') or str(ping.get('stdout') or '').strip() or 'redis_unavailable'

    chains = []
    for chain in PRODUCTION_CHAINS:
        cooldown_key = str(chain.get('cooldownKey') or '')
        status_key = str(chain.get('statusKey') or '')
        if redis_ok:
            cooldown = redis_cooldown_status(cooldown_key)
            probe = redis_probe_status(status_key)
        else:
            error = str(redis_info.get('error') or 'redis_unavailable')
            cooldown = {
                'available': False,
                'key': cooldown_key,
                'active': False,
                'ttlSeconds': 0,
                'error': error,
            }
            probe = {
                'available': False,
                'key': status_key,
                'ok': False,
                'state': 'unavailable',
                'error': error,
            }
        item = dict(chain)
        item['cooldown'] = cooldown
        item['probe'] = probe
        item['state'] = runtime_chain_state(cooldown, probe)
        chains.append(item)

    return {
        'generatedAt': generated_at,
        'redis': redis_info,
        'chains': chains,
    }


def trim_trailing_slash(url: str) -> str:
    return url.rstrip('/')


def summarize_probe_error(error: Any) -> str:
    if isinstance(error, dict):
        if isinstance(error.get('error'), dict):
            inner = error.get('error') or {}
            message = inner.get('message') or inner.get('type') or json.dumps(inner, ensure_ascii=False)
            return str(message)
        message = error.get('message') or error.get('detail') or error.get('error')
        if message:
            return str(message)
        return json.dumps(error, ensure_ascii=False)[:300]
    return str(error)[:300]


def test_upstream_connection(model_name: str, base_url: str, api_key: str, backend_model: str = 'openai/gpt-5.4') -> dict[str, Any]:
    model_name = model_name.strip()
    base_url = trim_trailing_slash(base_url.strip())
    api_key = api_key.strip()
    if not model_name or not base_url or not api_key:
        raise ValueError('missing_required_fields')

    headers = {
        'Authorization': f'Bearer {api_key}',
    }

    models_result = fetch_json(f'{base_url}/models', headers=headers, timeout=8)
    if models_result.get('ok'):
        body = models_result.get('body')
        available = []
        if isinstance(body, dict) and isinstance(body.get('data'), list):
            available = [str(item.get('id') or '') for item in body.get('data') if isinstance(item, dict)]
        detail = '/models ok'
        if available:
            sample = ', '.join([item for item in available[:3] if item])
            if sample:
                detail += f' | sample={sample}'
        return {
            'ok': True,
            'method': 'GET /models',
            'detail': detail,
            'status': models_result.get('status', 200),
        }

    fallback_payload = {
        'model': backend_model or 'openai/gpt-5.4',
        'messages': [
            {'role': 'user', 'content': 'ping'}
        ],
        'max_tokens': 1,
        'temperature': 0,
        'stream': False,
    }
    chat_result = post_json(f'{base_url}/chat/completions', fallback_payload, headers=headers, timeout=12)
    if chat_result.get('ok'):
        return {
            'ok': True,
            'method': 'POST /chat/completions',
            'detail': 'chat probe ok',
            'status': chat_result.get('status', 200),
        }

    models_error = summarize_probe_error(models_result.get('error'))
    chat_error = summarize_probe_error(chat_result.get('error'))
    raise ValueError(f'/models failed: {models_error} | /chat/completions failed: {chat_error}')


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


def get_litellm_config() -> dict[str, Any]:
    return load_yaml(LITELLM_CONFIG_PATH)


def normalize_entry_model(entry_model: str | None) -> str:
    raw = str(entry_model or '').strip()
    if not raw:
        return DEFAULT_ENTRY_MODEL
    normalized = raw.lower()
    for chain in PRODUCTION_CHAINS:
        chain_id = str(chain.get('id') or '').strip().lower()
        chain_entry = str(chain.get('entryModel') or '').strip().lower()
        if normalized in {chain_id, chain_entry}:
            return str(chain.get('entryModel') or '').strip()
    raise ValueError(f'invalid_entry_model:{raw}')


def entry_chain_meta(entry_model: str) -> dict[str, Any]:
    normalized = normalize_entry_model(entry_model)
    for chain in PRODUCTION_CHAINS:
        if str(chain.get('entryModel') or '').strip() == normalized:
            return dict(chain)
    return {
        'id': normalized,
        'label': normalized,
        'owner': 'Gateway',
        'entryModel': normalized,
    }


def entry_model_from_payload(payload: dict[str, Any]) -> str:
    return normalize_entry_model(
        str(payload.get('entryModel') or payload.get('modelFamily') or payload.get('chainId') or '').strip()
    )


def get_entry_backend_model(config: dict[str, Any], entry_model: str = DEFAULT_ENTRY_MODEL) -> str:
    entry_model = normalize_entry_model(entry_model)
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    for item in model_list:
        if not isinstance(item, dict):
            continue
        if str(item.get('model_name') or '').strip() != entry_model:
            continue
        params = item.get('litellm_params') if isinstance(item.get('litellm_params'), dict) else {}
        return str(params.get('model') or '').strip()
    return ''


def parse_os_environ_key(expr: str) -> str:
    prefix = 'os.environ/'
    if expr.startswith(prefix):
        return expr[len(prefix):]
    return ''


def get_dynamic_upstreams(config: dict[str, Any], env: dict[str, str], entry_model: str = DEFAULT_ENTRY_MODEL) -> dict[str, dict[str, str]]:
    entry_model = normalize_entry_model(entry_model)
    upstreams: dict[str, dict[str, str]] = {}
    entry_backend_model = get_entry_backend_model(config, entry_model)
    if not entry_backend_model:
        return upstreams
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    for item in model_list:
        if not isinstance(item, dict):
            continue
        model_name = str(item.get('model_name') or '').strip()
        if not model_name or model_name == entry_model:
            continue
        params = item.get('litellm_params') if isinstance(item.get('litellm_params'), dict) else {}
        backend_model = str(params.get('model') or '').strip()
        if backend_model != entry_backend_model:
            continue
        api_base_expr = str(params.get('api_base') or '')
        api_key_expr = str(params.get('api_key') or '')
        base_env_key = parse_os_environ_key(api_base_expr)
        api_key_env_key = parse_os_environ_key(api_key_expr)
        if not base_env_key:
            continue
        env_prefix = base_env_key.removesuffix('_UPSTREAM_BASE_URL')
        upstream_id = slugify_upstream_id(env_prefix.lower())
        suffix = 2
        while upstream_id in upstreams and upstreams[upstream_id].get('model_name') != model_name:
            upstream_id = f'{slugify_upstream_id(env_prefix.lower())}-{suffix}'
            suffix += 1
        upstreams[upstream_id] = {
            'env': env_prefix,
            'model_name': model_name,
            'base_url_env_key': base_env_key,
            'api_key_env_key': api_key_env_key,
            'base_url_value': env.get(base_env_key, ''),
            'api_key_value': env.get(api_key_env_key, ''),
        }
    return upstreams


def get_upstream_id_from_api_base(api_base: str, upstreams: dict[str, dict[str, str]]) -> str:
    env_key = parse_os_environ_key(api_base)
    env_prefix = env_key.removesuffix('_UPSTREAM_BASE_URL') if env_key else ''
    for upstream_id, meta in upstreams.items():
        if meta.get('env') == env_prefix:
            return upstream_id
    return ''


def get_upstream_fields(upstream_id: str, upstreams: dict[str, dict[str, str]]) -> dict[str, str]:
    meta = upstreams.get(upstream_id)
    if not meta:
        return {
            'baseUrlEnvKey': '',
            'apiKeyEnvKey': '',
            'baseUrlValue': '',
            'apiKeyMasked': '',
        }
    base_key = meta.get('base_url_env_key', '')
    api_key = meta.get('api_key_env_key', '')
    api_value = meta.get('api_key_value', '')
    masked = ''
    if api_value:
        prefix = api_value[:4]
        suffix = api_value[-4:] if len(api_value) > 8 else ''
        masked = f'{prefix}...{suffix}' if suffix else f'{prefix}...'
    return {
        'baseUrlEnvKey': base_key,
        'apiKeyEnvKey': api_key,
        'baseUrlValue': meta.get('base_url_value', ''),
        'apiKeyMasked': masked,
    }


def get_model_order(config: dict[str, Any], entry_model: str = DEFAULT_ENTRY_MODEL) -> dict[str, Any]:
    entry_model = normalize_entry_model(entry_model)
    chain_meta = entry_chain_meta(entry_model)
    env = parse_env(ENV_PATH)
    upstreams = get_dynamic_upstreams(config, env, entry_model)
    entry_backend_model = get_entry_backend_model(config, entry_model)
    scoped_model_names = {str(meta.get('model_name') or '') for meta in upstreams.values()}
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    router_settings = config.get('router_settings') if isinstance(config.get('router_settings'), dict) else {}
    fallbacks = router_settings.get('fallbacks') if isinstance(router_settings.get('fallbacks'), list) else []
    model_map: dict[str, dict[str, Any]] = {}
    name_to_upstream_id: dict[str, str] = {}
    for item in model_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get('model_name') or '').strip()
        if not name:
            continue
        if name != entry_model and name not in scoped_model_names:
            continue
        model_map[name] = item
        params = item.get('litellm_params') if isinstance(item.get('litellm_params'), dict) else {}
        api_base = str(params.get('api_base') or '')
        upstream_id = get_upstream_id_from_api_base(api_base, upstreams)
        if upstream_id:
            name_to_upstream_id[name] = upstream_id
    entry = model_map.get(entry_model, {})
    entry_upstream_id = ''
    if isinstance(entry, dict):
        params = entry.get('litellm_params') if isinstance(entry.get('litellm_params'), dict) else {}
        api_base = str(params.get('api_base') or '')
        entry_upstream_id = get_upstream_id_from_api_base(api_base, upstreams)
    fallback_chain = []
    for fb in fallbacks:
        if not isinstance(fb, dict):
            continue
        chain = fb.get(entry_model)
        if isinstance(chain, list):
            fallback_chain = [str(name) for name in chain if str(name).strip()]
            break
    active_aliases = []
    if entry_upstream_id:
        active_aliases.append(next((name for name, upstream_id in name_to_upstream_id.items() if upstream_id == entry_upstream_id and name != entry_model), ''))
    active_aliases.extend(fallback_chain)
    active_aliases = [name for name in active_aliases if name]
    ordered_models = []
    for name in active_aliases:
        item = model_map.get(name)
        if not item:
            continue
        params = item.get('litellm_params') if isinstance(item.get('litellm_params'), dict) else {}
        api_base = str(params.get('api_base') or '')
        upstream_id = get_upstream_id_from_api_base(api_base, upstreams)
        ordered_models.append({
            'upstreamId': upstream_id,
            'model_name': name,
            'enabled': True,
            'apiBase': api_base,
            'upstreamKey': upstreams.get(upstream_id, {}).get('env', ''),
            **get_upstream_fields(upstream_id, upstreams),
        })
    for upstream_id, meta in upstreams.items():
        name = next((model_name for model_name, mapped_upstream in name_to_upstream_id.items() if mapped_upstream == upstream_id and model_name != entry_model), '')
        if not name or name in {item['model_name'] for item in ordered_models}:
            continue
        item = model_map.get(name)
        if not item:
            continue
        params = item.get('litellm_params') if isinstance(item.get('litellm_params'), dict) else {}
        api_base = str(params.get('api_base') or '')
        ordered_models.append({
            'upstreamId': upstream_id,
            'model_name': name,
            'enabled': name in fallback_chain or upstream_id == entry_upstream_id,
            'apiBase': api_base,
            'upstreamKey': meta.get('env', ''),
            **get_upstream_fields(upstream_id, upstreams),
        })
    return {
        'entryModel': entry_model,
        'chainId': str(chain_meta.get('id') or entry_model),
        'label': str(chain_meta.get('label') or entry_model),
        'owner': str(chain_meta.get('owner') or 'Gateway'),
        'backendModel': entry_backend_model,
        'available': bool(entry_backend_model),
        'entryUpstreamId': entry_upstream_id,
        'fallbackChain': fallback_chain,
        'models': ordered_models,
        'rawFallbacks': fallbacks,
        'upstreams': upstreams,
    }


def clone_params_from_model(config: dict[str, Any], model_name: str) -> dict[str, Any]:
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    for item in model_list:
        if not isinstance(item, dict):
            continue
        if str(item.get('model_name') or '').strip() != model_name:
            continue
        params = item.get('litellm_params') if isinstance(item.get('litellm_params'), dict) else {}
        return json.loads(json.dumps(params))
    raise ValueError(f'model_not_found:{model_name}')


def get_all_model_orders(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [get_model_order(config, entry_model) for entry_model in EDITABLE_ENTRY_MODELS]


def build_router_config_payload(config: dict[str, Any], litellm_healthy: bool | None = None) -> dict[str, Any]:
    default_order = get_model_order(config, DEFAULT_ENTRY_MODEL)
    return {
        **default_order,
        'ok': True,
        'generatedAt': now_iso(),
        'configPath': str(LITELLM_CONFIG_PATH),
        'composePath': str(LITELLM_COMPOSE_PATH),
        'litellmHealthy': bool(litellm_healthy) if litellm_healthy is not None else bool(fetch_json(LITELLM_HEALTH_URL, timeout=5).get('ok')),
        'chains': get_all_model_orders(config),
    }


def get_model_name_by_upstream_id(config: dict[str, Any], upstream_id: str, entry_model: str = DEFAULT_ENTRY_MODEL) -> str:
    entry_model = normalize_entry_model(entry_model)
    env = parse_env(ENV_PATH)
    upstreams = get_dynamic_upstreams(config, env, entry_model)
    scoped_model_names = {str(meta.get('model_name') or '') for meta in upstreams.values()}
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    for item in model_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get('model_name') or '').strip()
        if name == entry_model or name not in scoped_model_names:
            continue
        params = item.get('litellm_params') if isinstance(item.get('litellm_params'), dict) else {}
        api_base = str(params.get('api_base') or '')
        if get_upstream_id_from_api_base(api_base, upstreams) == upstream_id:
            return name
    raise ValueError(f'upstream_not_found:{upstream_id}')


def write_model_order(entry_upstream_id: str, fallback_upstream_ids: list[str], entry_model: str = DEFAULT_ENTRY_MODEL) -> dict[str, Any]:
    entry_model = normalize_entry_model(entry_model)
    config = get_litellm_config()
    if not config:
        raise ValueError('config_unavailable')
    env = parse_env(ENV_PATH)
    upstreams = get_dynamic_upstreams(config, env, entry_model)
    if entry_upstream_id not in upstreams:
        raise ValueError('invalid_entry_upstream_id')
    invalid = [name for name in fallback_upstream_ids if name not in upstreams or name == entry_upstream_id]
    if invalid:
        raise ValueError('invalid_fallback_chain')
    if len(set(fallback_upstream_ids)) != len(fallback_upstream_ids):
        raise ValueError('duplicate_fallback_chain')
    entry_env_model = get_model_name_by_upstream_id(config, entry_upstream_id, entry_model)
    fallback_chain = [get_model_name_by_upstream_id(config, upstream_id, entry_model) for upstream_id in fallback_upstream_ids]
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    entry_params = clone_params_from_model(config, entry_env_model)
    updated_model_list = []
    for item in model_list:
        if not isinstance(item, dict):
            updated_model_list.append(item)
            continue
        if str(item.get('model_name') or '').strip() == entry_model:
            updated_item = json.loads(json.dumps(item))
            updated_item['litellm_params'] = entry_params
            updated_model_list.append(updated_item)
        else:
            updated_model_list.append(item)
    config['model_list'] = updated_model_list
    router_settings = config.get('router_settings') if isinstance(config.get('router_settings'), dict) else {}
    fallbacks = router_settings.get('fallbacks') if isinstance(router_settings.get('fallbacks'), list) else []
    replaced = False
    next_fallbacks = []
    for item in fallbacks:
        if isinstance(item, dict) and entry_model in item:
            next_fallbacks.append({entry_model: fallback_chain})
            replaced = True
        else:
            next_fallbacks.append(item)
    if not replaced:
        next_fallbacks.append({entry_model: fallback_chain})
    router_settings['fallbacks'] = next_fallbacks
    config['router_settings'] = router_settings
    dump_yaml(LITELLM_CONFIG_PATH, config)
    return get_model_order(config, entry_model)


def update_model_settings(upstream_id: str, new_model_name: str, base_url: str, api_key: str, entry_model: str = DEFAULT_ENTRY_MODEL) -> dict[str, Any]:
    entry_model = normalize_entry_model(entry_model)
    upstream_id = upstream_id.strip().lower()
    new_model_name = new_model_name.strip()
    base_url = base_url.strip()
    api_key = api_key.strip()
    if not new_model_name:
        raise ValueError('empty_model_name')
    config = get_litellm_config()
    if not config:
        raise ValueError('config_unavailable')
    env = parse_env(ENV_PATH)
    upstreams = get_dynamic_upstreams(config, env, entry_model)
    if upstream_id not in upstreams:
        raise ValueError('invalid_upstream_id')
    current_model_name = get_model_name_by_upstream_id(config, upstream_id, entry_model)
    if current_model_name == entry_model and new_model_name != current_model_name:
        raise ValueError('cannot_rename_entry_model')
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    for item in model_list:
        if not isinstance(item, dict):
            continue
        current_name = str(item.get('model_name') or '').strip()
        if current_name != current_model_name and current_name == new_model_name:
            raise ValueError('duplicate_model_name')
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    found = False
    for item in model_list:
        if not isinstance(item, dict):
            continue
        current_name = str(item.get('model_name') or '').strip()
        if current_name != current_model_name:
            continue
        item['model_name'] = new_model_name
        found = True
        break
    if not found:
        raise ValueError('model_not_found')
    router_settings = config.get('router_settings') if isinstance(config.get('router_settings'), dict) else {}
    fallbacks = router_settings.get('fallbacks') if isinstance(router_settings.get('fallbacks'), list) else []
    next_fallbacks = []
    for item in fallbacks:
        if not isinstance(item, dict):
            next_fallbacks.append(item)
            continue
        updated_item = {}
        for key, value in item.items():
            if key == entry_model and isinstance(value, list):
                updated_item[key] = [new_model_name if str(entry).strip() == current_model_name else entry for entry in value]
            else:
                updated_item[key] = value
        next_fallbacks.append(updated_item)
    router_settings['fallbacks'] = next_fallbacks
    config['router_settings'] = router_settings
    env_prefix = upstreams[upstream_id]['env']
    update_env_values(ENV_PATH, {
        f'{env_prefix}_UPSTREAM_BASE_URL': base_url,
        f'{env_prefix}_UPSTREAM_API_KEY': api_key,
    })
    entry_params = clone_params_from_model(config, new_model_name)
    for item in model_list:
        if not isinstance(item, dict):
            continue
        if str(item.get('model_name') or '').strip() == entry_model:
            params = item.get('litellm_params') if isinstance(item.get('litellm_params'), dict) else {}
            api_base_expr = str(params.get('api_base') or '')
            current_upstream = get_upstream_id_from_api_base(api_base_expr, upstreams)
            if current_upstream == upstream_id:
                item['litellm_params'] = entry_params
            break
    dump_yaml(LITELLM_CONFIG_PATH, config)
    return get_model_order(config, entry_model)


def next_available_env_prefix(env: dict[str, str], preferred_label: str) -> str:
    base = slugify_upstream_id(preferred_label).replace('-', '_').upper()
    if not base:
        base = 'CUSTOM'
    candidate = base
    index = 2
    while f'{candidate}_UPSTREAM_BASE_URL' in env or f'{candidate}_UPSTREAM_API_KEY' in env:
        candidate = f'{base}_{index}'
        index += 1
    return candidate


def add_new_model(model_name: str, base_url: str, api_key: str, entry_model: str = DEFAULT_ENTRY_MODEL) -> dict[str, Any]:
    entry_model = normalize_entry_model(entry_model)
    model_name = model_name.strip()
    base_url = base_url.strip()
    api_key = api_key.strip()
    if not model_name or not base_url or not api_key:
        raise ValueError('missing_required_fields')
    config = get_litellm_config()
    if not config:
        raise ValueError('config_unavailable')
    backend_model = get_entry_backend_model(config, entry_model)
    if not backend_model:
        raise ValueError('entry_model_not_found')
    env = parse_env(ENV_PATH)
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    for item in model_list:
        if not isinstance(item, dict):
            continue
        if str(item.get('model_name') or '').strip() == model_name:
            raise ValueError('duplicate_model_name')
    env_prefix = next_available_env_prefix(env, model_name)
    params = clone_params_from_model(config, entry_model)
    params['model'] = backend_model
    params['api_base'] = f'os.environ/{env_prefix}_UPSTREAM_BASE_URL'
    params['api_key'] = f'os.environ/{env_prefix}_UPSTREAM_API_KEY'
    new_item = {
        'model_name': model_name,
        'model_info': {
            'id': model_name,
        },
        'litellm_params': params,
    }
    model_list.append(new_item)
    config['model_list'] = model_list
    dump_yaml(LITELLM_CONFIG_PATH, config)
    update_env_values(ENV_PATH, {
        f'{env_prefix}_UPSTREAM_BASE_URL': base_url,
        f'{env_prefix}_UPSTREAM_API_KEY': api_key,
    })
    return get_model_order(config, entry_model)


def model_env_keys_in_use(config: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    for item in model_list:
        if not isinstance(item, dict):
            continue
        params = item.get('litellm_params') if isinstance(item.get('litellm_params'), dict) else {}
        for field in ('api_base', 'api_key'):
            key = parse_os_environ_key(str(params.get(field) or ''))
            if key:
                keys.add(key)
    return keys


def delete_model(upstream_id: str, entry_model: str = DEFAULT_ENTRY_MODEL) -> dict[str, Any]:
    entry_model = normalize_entry_model(entry_model)
    upstream_id = upstream_id.strip().lower()
    config = get_litellm_config()
    if not config:
        raise ValueError('config_unavailable')
    order = get_model_order(config, entry_model)
    target = next((item for item in order.get('models') or [] if str(item.get('upstreamId') or '') == upstream_id), None)
    if not target:
        raise ValueError('invalid_upstream_id')
    if bool(target.get('enabled')):
        raise ValueError('cannot_delete_active_model')
    target_model_name = str(target.get('model_name') or '').strip()
    if not target_model_name:
        raise ValueError('model_not_found')
    model_list = config.get('model_list') if isinstance(config.get('model_list'), list) else []
    next_model_list = [
        item for item in model_list
        if not (isinstance(item, dict) and str(item.get('model_name') or '').strip() == target_model_name)
    ]
    if len(next_model_list) == len(model_list):
        raise ValueError('model_not_found')
    config['model_list'] = next_model_list
    router_settings = config.get('router_settings') if isinstance(config.get('router_settings'), dict) else {}
    fallbacks = router_settings.get('fallbacks') if isinstance(router_settings.get('fallbacks'), list) else []
    next_fallbacks = []
    for item in fallbacks:
        if not isinstance(item, dict):
            next_fallbacks.append(item)
            continue
        updated_item = {}
        for key, value in item.items():
            if key == entry_model and key == target_model_name:
                continue
            if key == entry_model and isinstance(value, list):
                updated_item[key] = [entry for entry in value if str(entry).strip() != target_model_name]
            else:
                updated_item[key] = value
        next_fallbacks.append(updated_item)
    router_settings['fallbacks'] = next_fallbacks
    config['router_settings'] = router_settings
    dump_yaml(LITELLM_CONFIG_PATH, config)
    remaining_env_keys = model_env_keys_in_use(config)
    remove_env_keys(ENV_PATH, [
        key for key in [
            str(target.get('baseUrlEnvKey') or ''),
            str(target.get('apiKeyEnvKey') or ''),
        ]
        if key and key not in remaining_env_keys
    ])
    return get_model_order(config, entry_model)


def restart_litellm() -> dict[str, Any]:
    completed = subprocess.run(
        [DOCKER_BIN, 'restart', LITELLM_RESTART_CONTAINER],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    health = {'ok': False, 'status': 0, 'error': 'health_check_not_started'}
    for _ in range(8):
        health = fetch_json(LITELLM_HEALTH_URL, timeout=5)
        if health.get('ok'):
            break
        time.sleep(1)
    return {
        'ok': completed.returncode == 0 and bool(health.get('ok')),
        'returncode': completed.returncode,
        'container': LITELLM_RESTART_CONTAINER,
        'stdout': completed.stdout.strip(),
        'stderr': completed.stderr.strip(),
        'health': health,
    }


def build_status() -> dict[str, Any]:
    env = parse_env(ENV_PATH)
    watcher_state = load_json(WATCHER_STATE_PATH)
    chrome = sync.get_chrome_account()
    profile_load_error = ''
    try:
        profiles = sync.load_profiles()
    except (Exception, SystemExit) as exc:
        profiles = {}
        profile_load_error = str(exc)

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
    except (Exception, SystemExit) as exc:
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

    if env_oauth_access:
        try:
            quota = quota_mod.fetch_quota(env_oauth_access, env_account_id)
        except (Exception, SystemExit) as exc:
            quota = {
                'ok': False,
                'status': 0,
                'error': str(exc),
            }
    else:
        quota = {
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
    runtime = build_runtime_status()

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
            'profileLoadError': profile_load_error,
        },
        'litellmBinding': env_binding,
        'watcher': watcher_state,
        'quota': quota,
        'litellm': {
            'healthUrl': LITELLM_HEALTH_URL,
            'health': litellm_health,
        },
        'runtime': runtime,
        'consistency': consistency,
        'summary': summary,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get('Content-Length') or '0')
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode('utf-8'))
        return parsed if isinstance(parsed, dict) else {}

    def _query_params(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _path_only(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def do_GET(self) -> None:
        path = self._path_only()
        if path == '/':
            self._send_html(UI_HTML)
            return
        if path == '/failover-stats':
            self._send_json(proxy_failover_stats(self.path))
            return
        if path == '/status':
            self._send_json(build_status())
            return
        if path == '/router-config':
            config = get_litellm_config()
            status = build_status()
            payload = build_router_config_payload(config, bool(status.get('litellm', {}).get('health', {}).get('ok')))
            params = self._query_params()
            requested = (params.get('entryModel') or params.get('modelFamily') or params.get('chainId') or [''])[0]
            if requested:
                order = get_model_order(config, requested)
                payload.update(order)
            self._send_json(payload)
            return
        if path == '/summary':
            status = build_status()
            self._send_json(status.get('summary') if isinstance(status.get('summary'), dict) else {'ok': False, 'error': 'summary_unavailable'})
            return
        if path == '/summary.txt':
            status = build_status()
            text = str(((status.get('summary') or {}).get('text')) or '')
            body = (text + '\n').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/healthz':
            status = build_status()
            ok = bool(status.get('litellm', {}).get('health', {}).get('ok'))
            self._send_json({'ok': ok, 'generatedAt': status.get('generatedAt'), 'shouldResyncLiteLLM': status.get('consistency', {}).get('shouldResyncLiteLLM')}, 200 if ok else 503)
            return
        if path == '/quota':
            status = build_status()
            self._send_json(status.get('quota') if isinstance(status.get('quota'), dict) else {'ok': False, 'error': 'quota_unavailable'})
            return
        self._send_json({'ok': False, 'error': 'not_found', 'path': self.path}, 404)

    def do_POST(self) -> None:
        path = self._path_only()
        if path == '/failover-stats/reset':
            result = reset_failover_stats()
            self._send_json(result, 200 if result.get('ok') else 500)
            return
        if path == '/router-config/model/test':
            try:
                payload = self._read_json_body()
                entry_model = entry_model_from_payload(payload)
                model_name = str(payload.get('modelName') or '').strip()
                base_url = str(payload.get('baseUrl') or '').strip()
                api_key = str(payload.get('apiKey') or '').strip()
                backend_model = get_entry_backend_model(get_litellm_config(), entry_model) or f'openai/{entry_model}'
                probe = test_upstream_connection(model_name, base_url, api_key, backend_model)
                self._send_json({
                    'ok': True,
                    'message': 'model_probe_ok',
                    'probe': probe,
                }, status=200)
            except Exception as exc:
                self._send_json({'ok': False, 'error': str(exc)}, 400)
            return
        if path == '/router-config/model/delete':
            try:
                payload = self._read_json_body()
                entry_model = entry_model_from_payload(payload)
                upstream_id = str(payload.get('upstreamId') or '').strip().lower()
                order = delete_model(upstream_id, entry_model)
                restart = restart_litellm()
                status_code = 200 if restart.get('ok') else 500
                self._send_json({
                    'ok': bool(restart.get('ok')),
                    'message': 'model_deleted_and_restarted' if restart.get('ok') else 'model_deleted_but_restart_failed',
                    'restart': restart,
                    **order,
                }, status=status_code)
            except Exception as exc:
                self._send_json({'ok': False, 'error': str(exc)}, 400)
            return
        if path == '/router-config/model/new':
            try:
                payload = self._read_json_body()
                entry_model = entry_model_from_payload(payload)
                model_name = str(payload.get('modelName') or '').strip()
                base_url = str(payload.get('baseUrl') or '').strip()
                api_key = str(payload.get('apiKey') or '').strip()
                order = add_new_model(model_name, base_url, api_key, entry_model)
                restart = restart_litellm()
                status_code = 200 if restart.get('ok') else 500
                self._send_json({
                    'ok': bool(restart.get('ok')),
                    'message': 'model_created_and_restarted' if restart.get('ok') else 'model_created_but_restart_failed',
                    'restart': restart,
                    **order,
                }, status=status_code)
            except Exception as exc:
                self._send_json({'ok': False, 'error': str(exc)}, 400)
            return
        if path == '/router-config/model':
            try:
                payload = self._read_json_body()
                entry_model = entry_model_from_payload(payload)
                upstream_id = str(payload.get('upstreamId') or '').strip().lower()
                model_name = str(payload.get('modelName') or '').strip()
                base_url = str(payload.get('baseUrl') or '').strip()
                api_key = str(payload.get('apiKey') or '').strip()
                order = update_model_settings(upstream_id, model_name, base_url, api_key, entry_model)
                restart = restart_litellm()
                status_code = 200 if restart.get('ok') else 500
                self._send_json({
                    'ok': bool(restart.get('ok')),
                    'message': 'model_updated_and_restarted' if restart.get('ok') else 'model_updated_but_restart_failed',
                    'restart': restart,
                    **order,
                }, status=status_code)
            except Exception as exc:
                self._send_json({'ok': False, 'error': str(exc)}, 400)
            return
        if path != '/router-config':
            self._send_json({'ok': False, 'error': 'not_found', 'path': self.path}, 404)
            return
        try:
            payload = self._read_json_body()
            entry_model = entry_model_from_payload(payload)
            entry_upstream_id = str(payload.get('entryUpstreamId') or '').strip().lower()
            fallback_chain = payload.get('fallbackChain')
            if not isinstance(fallback_chain, list):
                raise ValueError('fallback_chain_must_be_list')
            fallback_chain = [str(item).strip().lower() for item in fallback_chain if str(item).strip()]
            order = write_model_order(entry_upstream_id, fallback_chain, entry_model)
            restart = restart_litellm()
            status_code = 200 if restart.get('ok') else 500
            self._send_json({
                'ok': bool(restart.get('ok')),
                'message': 'config_updated_and_restarted' if restart.get('ok') else 'config_updated_but_restart_failed',
                'restart': restart,
                **order,
            }, status=status_code)
        except Exception as exc:
            self._send_json({'ok': False, 'error': str(exc)}, 400)


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
