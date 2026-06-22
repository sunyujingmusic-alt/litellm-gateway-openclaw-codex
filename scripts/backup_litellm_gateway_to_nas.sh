#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAS_ROOT_DEFAULT="${BACKUP_ROOT:-$ROOT/backups}"
TS=$(date +%Y%m%d-%H%M%S)
INCLUDE_SECRETS=0
NAS_ROOT="$NAS_ROOT_DEFAULT"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nas-root)
      NAS_ROOT="$2"
      shift 2
      ;;
    --include-secrets)
      INCLUDE_SECRETS=1
      shift
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "$NAS_ROOT" ]]; then
  mkdir -p "$NAS_ROOT"
fi

DEST="$NAS_ROOT/backup-$TS"
SRC_DEST="$DEST/source"
DOC_DEST="$DEST/docs"
META_DEST="$DEST/meta"
mkdir -p "$SRC_DEST" "$DOC_DEST" "$META_DEST"

RSYNC_EXCLUDES=(
  --exclude '.git/'
  --exclude '.venv/'
  --exclude '__pycache__/'
  --exclude 'logs/'
  --exclude 'tmp/'
  --exclude '*.pyc'
)

if [[ "$INCLUDE_SECRETS" -eq 0 ]]; then
  RSYNC_EXCLUDES+=(
    --exclude '.env'
    --exclude '.env.codex-oauth-gmn.test'
  )
fi

rsync -a "${RSYNC_EXCLUDES[@]}" "$ROOT/" "$SRC_DEST/"

cat > "$META_DEST/backup-info.json" <<EOF
{
  "createdAt": "$(date -Iseconds)",
  "sourceRoot": "$ROOT",
  "nasRoot": "$NAS_ROOT",
  "backupDir": "$DEST",
  "includeSecrets": $INCLUDE_SECRETS,
  "notes": "Default backup excludes live .env files, tmp, logs, venv, and pycache."
}
EOF

(
  cd "$DEST"
  tar -czf "litellm-gateway-source-$TS.tar.gz" source docs meta
  shasum -a 256 "litellm-gateway-source-$TS.tar.gz" > "litellm-gateway-source-$TS.tar.gz.sha256"
)

printf 'backup_dir=%s\n' "$DEST"
printf 'archive=%s\n' "$DEST/litellm-gateway-source-$TS.tar.gz"
printf 'sha256=%s\n' "$DEST/litellm-gateway-source-$TS.tar.gz.sha256"
printf 'include_secrets=%s\n' "$INCLUDE_SECRETS"
