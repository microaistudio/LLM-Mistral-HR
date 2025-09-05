#!/usr/bin/env bash
# llama-server.sh — run llama.cpp server as a persistent local model daemon
# Usage: ./llama-server.sh start|stop|status|logs
set -Eeuo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a; [[ -f "$DIR/.env" ]] && source "$DIR/.env"; set +a

LLAMA_SERVER_BIN="${LLAMA_SERVER_BIN:-${LLAMA_BIN%/*}/llama-server}"  # defaults next to llama-run
MODEL_PATH="${MODEL_PATH:?MODEL_PATH not set}"
PORT="${LLAMA_SERVER_PORT:-8081}"
HOST="${LLAMA_SERVER_HOST:-127.0.0.1}"
THREADS="${MODEL_THREADS:-4}"
CTX="${MODEL_CTX:-4096}"

LOG_DIR="$DIR/logs"; mkdir -p "$LOG_DIR"
PIDFILE="$DIR/.llama_server.pid"
LOGFILE="$LOG_DIR/llama-server.out"

is_running(){ [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start(){
  if is_running; then echo "✅ llama-server already running (PID $(cat "$PIDFILE"))"; exit 0; fi
  [[ -x "$LLAMA_SERVER_BIN" ]] || { echo "❌ Not found: $LLAMA_SERVER_BIN"; exit 1; }
  [[ -r "$MODEL_PATH" ]] || { echo "❌ MODEL_PATH not readable: $MODEL_PATH"; exit 1; }
  echo "▶ Starting llama-server on http://$HOST:$PORT ..."
  nohup "$LLAMA_SERVER_BIN" \
    -m "$MODEL_PATH" -t "$THREADS" -c "$CTX" \
    --host "$HOST" --port "$PORT" \
    --parallel 2 --cont-batching \
    --timeout 600000 \
    >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  echo "PID $(cat "$PIDFILE") — logs: $LOGFILE"
}

stop(){
  if ! is_running; then echo "⛔ not running"; rm -f "$PIDFILE"; exit 0; fi
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  for _ in {1..25}; do kill -0 "$(cat "$PIDFILE")" 2>/dev/null || { rm -f "$PIDFILE"; echo "🛑 stopped"; exit 0; }; sleep 0.2; done
  kill -9 "$(cat "$PIDFILE")" 2>/dev/null || true; rm -f "$PIDFILE"; echo "🛑 forced stop"
}

status(){ is_running && echo "✅ running (PID $(cat "$PIDFILE")) on $HOST:$PORT" || echo "⛔ not running"; }
logs(){ [[ -f "$LOGFILE" ]] || touch "$LOGFILE"; echo "Tailing $LOGFILE"; tail -f "$LOGFILE"; }

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  logs) logs ;;
  *) echo "Usage: $0 {start|stop|status|logs}"; exit 1 ;;
esac
