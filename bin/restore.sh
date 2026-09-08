#!/usr/bin/env bash
# bin/restore.sh — Restore workbench data from a backup created by bin/backup.sh
#
# Usage:
#   ./bin/restore.sh backups/ai-research-workbench_fox_data-20250101-120000.tar.gz
#   ./bin/restore.sh --latest   # restores the most recent fox_data backup

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$ROOT/backups"

if [[ "${1:-}" == "--latest" ]]; then
  file=$(ls -t "$BACKUP_DIR"/ai-research-workbench_fox_data-*.tar.gz 2>/dev/null | head -n 1)
  if [[ -z "$file" ]]; then echo "No fox_data backup found in $BACKUP_DIR"; exit 1; fi
  set -- "$file"
fi

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 <backup.tar.gz>  or  $0 --latest"
  echo "Available backups:"
  ls -1 "$BACKUP_DIR"/*.tar.gz 2>/dev/null | head -n 20 || echo "  (none)"
  exit 1
fi

BACKUP_FILE="$1"
if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "Not found: $BACKUP_FILE"
  exit 1
fi

# Infer volume name from filename (ai-research-workbench_fox_data-20250101.tar.gz -> ai-research-workbench_fox_data)
VOL=$(basename "$BACKUP_FILE" | sed -E 's/-[0-9]{8}-[0-9]{6}\.tar\.gz$//; s/\.tar\.gz$//')
if [[ "$VOL" == "personal-experiments" || "$VOL" == "hive-dotfiles" ]]; then
  echo "==> Restoring host bind mount $VOL from $BACKUP_FILE ..."
  if [[ "$VOL" == "personal-experiments" ]]; then
    tar xzf "$BACKUP_FILE" -C "$ROOT/.." 2>/dev/null
    echo "  Restored to $ROOT/../personal-experiments"
  else
    tar xzf "$BACKUP_FILE" -C "$HOME" 2>/dev/null
    echo "  Restored to $HOME/.hive"
  fi
  exit 0
fi

echo "==> Restoring Docker volume $VOL from $BACKUP_FILE ..."
if ! docker volume inspect "$VOL" >/dev/null 2>&1; then
  echo "  Creating volume $VOL ..."
  docker volume create "$VOL" >/dev/null
fi
echo "  Restoring to $VOL (this will overwrite)..."
docker run --rm -v "$VOL:/volume" -v "$(dirname "$BACKUP_FILE"):/backup:ro" alpine sh -c "rm -rf /volume/* /volume/.* 2>/dev/null || true; tar xzf /backup/$(basename "$BACKUP_FILE") -C /volume"
echo "  Done. Verify with: docker run --rm -v $VOL:/volume alpine ls -lh /volume | head"

# Also ensure the volume is labelled as persistent
docker volume inspect "$VOL" --format '{{json .Labels}}' 2>/dev/null | grep -q "com.fox.workbench" || \
  echo "  Note: volume $VOL was restored but may need 'docker volume create --label com.fox.workbench/persist=true $VOL' on next recreation (compose will handle)."
