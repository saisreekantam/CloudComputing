#!/usr/bin/env bash
# Self-hosted LLM on a remote GPU box, reached over SSH.
#
# The stack talks to any OpenAI-compatible endpoint. With Ollama on a GPU server, the workers use
#   LLM_BASE_URL=http://host.docker.internal:${LOCAL_PORT}/v1
# and this script keeps that local port forwarded to the server, reconnecting automatically when the
# laptop sleeps or the network drops (a dead tunnel otherwise turns every job into retries → DLQ).
#
#   export GPU_HOST=user@gpu-server
#   scripts/gpu_tunnel.sh gpus             # load per GPU (pick the quietest one for `serve`)
#   scripts/gpu_tunnel.sh serve 2          # optional: private Ollama pinned to GPU 2 (shared boxes)
#   scripts/gpu_tunnel.sh up               # forward + auto-reconnect in the background
#   scripts/gpu_tunnel.sh check            # endpoint reachable? model loaded on GPU?
#   scripts/gpu_tunnel.sh down             # stop the tunnel
#
# Env: GPU_HOST (required), LOCAL_PORT=11435, REMOTE_PORT=11436 (private server; 11434 = system Ollama),
#      LLM_MODEL=nt-qwen3vl-30b, OLLAMA_MODELS_DIR (reuse an existing model store, read-only).
set -euo pipefail

LOCAL_PORT=${LOCAL_PORT:-11435}
REMOTE_PORT=${REMOTE_PORT:-11436}
MODEL=${LLM_MODEL:-nt-qwen3vl-30b}
PIDFILE=${TMPDIR:-/tmp}/notetaker_gpu_tunnel.pid
LOG=${TMPDIR:-/tmp}/notetaker_gpu_tunnel.log
SSH_OPTS=(-o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3)

need_host() { : "${GPU_HOST:?set GPU_HOST=user@host (key-based SSH)}"; }

cmd_gpus() {
  need_host
  # Throughput on a shared server is dominated by other users' load: pick the least busy GPU
  # with >= 24 GB free (the 30B-A3B model needs ~20 GB + KV cache).
  ssh "${SSH_OPTS[@]}" "$GPU_HOST" "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total --format=csv,noheader" |
    awk -F', ' '{gsub(/ MiB/,"",$4); gsub(/ MiB/,"",$5); printf "GPU %s  %-45s util %5s  free %6.1f GB\n", $1, $2, $3, ($5-$4)/1024}'
}

cmd_serve() {
  need_host
  local gpu=${1:?usage: serve <gpu-index>}
  local models=""
  [[ -n "${OLLAMA_MODELS_DIR:-}" ]] && models="OLLAMA_MODELS=${OLLAMA_MODELS_DIR}"
  echo "starting private Ollama on ${GPU_HOST} GPU ${gpu}, port ${REMOTE_PORT}"
  # Only this user's own `ollama` processes are stopped (exact process name + owner, not a command-line match).
  ssh "${SSH_OPTS[@]}" "$GPU_HOST" "pkill -u \"\$(id -un)\" -x ollama || true; sleep 2;
    CUDA_VISIBLE_DEVICES=${gpu} OLLAMA_VULKAN=0 GGML_VK_VISIBLE_DEVICES= OLLAMA_HOST=127.0.0.1:${REMOTE_PORT} \
    ${models} OLLAMA_NOPRUNE=1 OLLAMA_NUM_PARALLEL=4 OLLAMA_KEEP_ALIVE=30m \
    setsid nohup ollama serve > ~/nt_ollama.log 2>&1 < /dev/null &
    sleep 4
    curl -sf 127.0.0.1:${REMOTE_PORT}/api/tags | grep -q '\"${MODEL}' \
      && echo 'model ${MODEL} available' \
      || echo 'model ${MODEL} missing: see deploy/ollama/Modelfile (ollama pull + ollama create)'"
}

loop() {
  while true; do
    ssh "${SSH_OPTS[@]}" -N -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$GPU_HOST" || true
    echo "$(date '+%F %T') tunnel dropped; reconnecting in 3s" >&2
    sleep 3
  done
}

cmd_up() {
  need_host
  if [[ "${1:-}" == "--foreground" ]]; then loop; return; fi
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "tunnel already running (pid $(cat "$PIDFILE"))"; return
  fi
  nohup "$0" up --foreground >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  sleep 2
  echo "tunnel localhost:${LOCAL_PORT} -> ${GPU_HOST}:${REMOTE_PORT} (pid $(cat "$PIDFILE"), log $LOG)"
}

cmd_down() {
  if [[ -f "$PIDFILE" ]]; then
    local pid; pid=$(cat "$PIDFILE")
    pkill -P "$pid" 2>/dev/null || true   # the ssh child of the reconnect loop
    kill "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "tunnel stopped"
  else
    echo "no tunnel pidfile"
  fi
}

cmd_check() {
  if curl -sf -m 5 "http://localhost:${LOCAL_PORT}/v1/models" | grep -q "\"${MODEL}"; then
    echo "OK  endpoint http://localhost:${LOCAL_PORT}/v1 serves ${MODEL}"
  else
    echo "FAIL endpoint http://localhost:${LOCAL_PORT}/v1 unreachable or ${MODEL} missing"; exit 1
  fi
  if [[ -n "${GPU_HOST:-}" ]]; then
    local prog
    prog=$(cat <<'PY'
import json, sys
ms = json.load(sys.stdin).get("models", [])
parts = ["%s %d%% on GPU" % (m["name"], m["size_vram"] * 100 // max(m["size"], 1)) for m in ms]
print("    loaded:", ", ".join(parts) or "nothing yet (loads on first call)")
PY
)
    ssh "${SSH_OPTS[@]}" "$GPU_HOST" "curl -s 127.0.0.1:${REMOTE_PORT}/api/ps" | python3 -c "$prog"
  fi
}

case "${1:-}" in
  gpus) cmd_gpus ;;
  serve) shift; cmd_serve "$@" ;;
  up) shift; cmd_up "$@" ;;
  down) cmd_down ;;
  check) cmd_check ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
