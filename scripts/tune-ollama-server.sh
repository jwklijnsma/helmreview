#!/usr/bin/env bash
# Tune an Ollama server (systemd) for helmreview throughput. Run ON the Ollama host as root.
#
#   sudo ./tune-ollama-server.sh                # 2 parallel slots
#   sudo ./tune-ollama-server.sh --parallel 3   # more slots (needs more VRAM)
#   sudo ./tune-ollama-server.sh --dry-run      # only print what it would do
#
# What it sets, and why:
#   OLLAMA_FLASH_ATTENTION=1   faster prompt processing, much less VRAM for the context
#   OLLAMA_KV_CACHE_TYPE=q8_0  halves KV-cache VRAM at no visible quality loss, so the
#                              whole model + context stays on the GPU (no CPU offload)
#   OLLAMA_NUM_PARALLEL=N      N requests at once; then run helmreview with --workers N
#   OLLAMA_KEEP_ALIVE=30m      no reload between requests
#   OLLAMA_MAX_LOADED_MODELS=1 one model owns the GPU (another model would force offload)
#
# VRAM rule of thumb for qwen2.5-coder:14b (q4, ~9 GiB) with q8 KV cache:
#   ~1.5 GiB per parallel slot at num_ctx 16384 -> 2 slots ~13 GiB, 3 slots ~15 GiB.
# Afterwards check with: helmreview doctor --host http://<this-host>:11434

set -euo pipefail

PARALLEL=2
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --parallel) PARALLEL="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

ENV_LINES="Environment=\"OLLAMA_FLASH_ATTENTION=1\"
Environment=\"OLLAMA_KV_CACHE_TYPE=q8_0\"
Environment=\"OLLAMA_NUM_PARALLEL=${PARALLEL}\"
Environment=\"OLLAMA_KEEP_ALIVE=30m\"
Environment=\"OLLAMA_MAX_LOADED_MODELS=1\""

if ! command -v systemctl >/dev/null || ! systemctl cat ollama.service >/dev/null 2>&1; then
  echo "No ollama systemd service found. Set these in the environment of 'ollama serve'"
  echo "(Docker: -e flags; Proxmox LXC/other: the service's env file):"
  echo "$ENV_LINES" | sed 's/^Environment="\(.*\)"$/  \1/'
  exit 0
fi

DROPIN=/etc/systemd/system/ollama.service.d/helmreview.conf
CONTENT="# written by helmreview scripts/tune-ollama-server.sh
[Service]
$ENV_LINES"

echo "Writing $DROPIN:"
echo "$CONTENT"
if [ "$DRY" = 1 ]; then echo "(dry run, nothing changed)"; exit 0; fi
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

mkdir -p "$(dirname "$DROPIN")"
printf '%s\n' "$CONTENT" > "$DROPIN"
systemctl daemon-reload
systemctl restart ollama
sleep 2
systemctl is-active --quiet ollama && echo "✓ ollama restarted" || { echo "✗ ollama failed to start; see: journalctl -u ollama" >&2; exit 1; }
if command -v nvidia-smi >/dev/null; then
  nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
fi
echo "Now run helmreview with: --workers ${PARALLEL}   (or workers: ${PARALLEL} in ~/.helmreview.yml)"
