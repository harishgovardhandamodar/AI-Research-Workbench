#!/usr/bin/env bash
# bin/backup.sh — Persist all workbench data against accidental docker resets
#
# Backs up every persistent volume (fox_data, hive_workspace, etc.) plus host
# bind mounts (personal-experiments, ~/.hive) to ./backups/<timestamp>.tar.gz.
# Restores with bin/restore.sh. Safe to run before `docker compose down -v`
# or `docker system prune --volumes`.
#
# Usage:
#   ./bin/backup.sh              # backup all volumes to ./backups/
#   ./bin/backup.sh --list       # list existing backups
#   ./bin/backup.sh --verify <file>  # verify a backup

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$ROOT/backups"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
VOLUMES=(ai-research-workbench_fox_data ai-research-workbench_hive_workspace ai-research-workbench_ollama_data ai-research-workbench_code_server_config)

mkdir -p "$BACKUP_DIR"

if [[ "${1:-}" == "--list" ]]; then
  ls -lh "$BACKUP_DIR"/*.tar.gz 2>/dev/null || echo "No backups in $BACKUP_DIR"
  exit 0
fi
if [[ "${1:-}" == "--verify" && -n "${2:-}" ]]; then
  tar tzf "$2" >/dev/null && echo "OK: $2" || echo "FAIL: $2"
  exit 0
fi

echo "==> Backing up Docker volumes to $BACKUP_DIR/ ..."

for vol in "${VOLUMES[@]}"; do
  if docker volume inspect "$vol" >/dev/null 2>&1; then
    out="$BACKUP_DIR/${vol}-$TIMESTAMP.tar.gz"
    echo "  - $vol -> $(basename "$out")"
    docker run --rm -v "$vol:/volume:ro" -v "$BACKUP_DIR:/backup" alpine tar czf "/backup/$(basename "$out")" -C /volume . 2>/dev/null
  else
    echo "  - $vol (not found, skipping)"
  fi
done

# Also snapshot host bind mounts (personal-experiments, ~/.hive) if they exist
if [[ -d "$ROOT/../personal-experiments" ]]; then
  echo "  - personal-experiments -> personal-experiments-$TIMESTAMP.tar.gz"
  tar czf "$BACKUP_DIR/personal-experiments-$TIMESTAMP.tar.gz" -C "$ROOT/.." personal-experiments 2>/dev/null || true
fi
if [[ -d "$HOME/.hive" ]]; then
  echo "  - ~/.hive -> hive-dotfiles-$TIMESTAMP.tar.gz"
  tar czf "$BACKUP_DIR/hive-dotfiles-$TIMESTAMP.tar.gz" -C "$HOME" .hive 2>/dev/null || true
fi

echo "==> Done. Backups in $BACKUP_DIR:"
ls -lh "$BACKUP_DIR"/*"$TIMESTAMP".tar.gz 2>/dev/null | awk '{print "  " $9 " (" $5 ")"}'
echo ""
echo "To restore: ./bin/restore.sh $BACKUP_DIR/<file>.tar.gz"
echo "To prevent accidental deletes: NEVER run 'docker compose down -v' or 'docker system prune --volumes' without a backup."
echo "Volumes are labelled com.fox.workbench/persist=true — 'docker volume ls --filter label=com.fox.workbench/persist=true' to verify."
