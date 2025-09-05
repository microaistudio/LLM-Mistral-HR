#!/usr/bin/env bash
# llm.sh — one-touch launcher for the Mistral LLM gateway (FastAPI + llama.cpp)
# Usage:
#   ./llm.sh start      # background, no reload, logs to logs/llm.out
#   ./llm.sh dev        # foreground, auto-reload (developer mode)
#   ./llm.sh stop       # stop background server
#   ./llm.sh restart    # stop then start
#   ./llm.sh status     # show running status
#   ./llm.sh logs       # tail logs
#   ./llm.sh warmup     # run a quick llama warmup (CLI)
#   ./llm.sh debug      # GET /debug/llm
#   ./llm.sh test       # GET /chat?q=hi
#
# Notes:
# - Reads .env (LLAMA_BIN, MODEL_PATH, etc). Sets safe defaults if missing:
#     LLAMA_TIMEOUT=600, LLAMA_WARMUP=1
# - Uses venv at ./venv. Exits with a helpful hint if not found.

set -Eeuo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$DIR/venv"
PY="$VENV_DIR/bin/python"
UVICORN_CMD=("$PY" -m uvicorn)
APP="server:app"

# Network
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# Files
LOG_DIR="$DIR/logs"
PIDFILE="$DIR/.llm_uvicorn.pid"
LOGFILE="$LOG_DIR/llm.out"

# --- helpers ---
die() { echo "❌ $*" >&2; exit 1; }
note() { echo "👉 $*"; }

ensure_dirs() { mkdir -p "$LOG_DIR"; }

ensure_venv() {
  [[ -x "$PY" ]] || die "Python venv not found at $VENV_DIR. Create it:  python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
}

load_env() {
  set +u
  if [[ -f "$DIR/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "$DIR/.env"; set +a
  fi
  set -u
  export LLAMA_TIMEOUT="${LLAMA_TIMEOUT:-600}"
  export LLAMA_WARMUP="${LLAMA_WARMUP:-1}"
}

check_llama_vars() {
  [[ -n "${LLAMA_BIN:-}" ]] || die "LLAMA_BIN is not set (from .env)."
  [[ -n "${MODEL_PATH:-}" ]] || die "MODEL_PATH is not set (from .env)."
  [[ -x "$LLAMA_BIN" ]] || die "LLAMA_BIN is not executable: $LLAMA_BIN"
  [[ -r "$MODEL_PATH" ]] || die "MODEL_PATH not readable: $MODEL_PATH"
}

is_running() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

start_prod() {
  if is_running; then
    note "Already running (PID $(cat "$PIDFILE"))."
    exit 0
  fi
  ensure_dirs
  ensure_venv
  load_env
  check_llama_vars

  note "Starting FastAPI (prod) on http://$HOST:$PORT ..."
  # Run in background with nohup; write PID
  nohup "${UVICORN_CMD[@]}" "$APP" --host "$HOST" --port "$PORT" \
    >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  note "PID $(cat "$PIDFILE") — logs: $LOGFILE"
}

start_dev() {
  ensure_venv
  load_env
  check_llama_vars
  note "Starting FastAPI (dev --reload) on http://$HOST:$PORT ..."
  exec "${UVICORN_CMD[@]}" "$APP" --host "$HOST" --port "$PORT" --reload
}

stop_srv() {
  if ! is_running; then
    note "Not running."
    rm -f "$PIDFILE" >/dev/null 2>&1 || true
    exit 0
  fi
  local pid; pid="$(cat "$PIDFILE")"
  note "Stopping PID $pid ..."
  kill "$pid" 2>/dev/null || true
  # wait a bit, then force if needed
  for i in {1..20}; do
    kill -0 "$pid" 2>/dev/null || { rm -f "$PIDFILE"; note "Stopped."; return; }
    sleep 0.2
  done
  note "Force killing $pid ..."
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
  note "Stopped."
}

status_srv() {
  if is_running; then
    echo "✅ Running (PID $(cat "$PIDFILE")), port $PORT"
  else
    echo "⛔ Not running"
  fi
}

logs_tail() {
  ensure_dirs
  [[ -f "$LOGFILE" ]] || touch "$LOGFILE"
  note "Tailing $LOGFILE (Ctrl+C to exit)"
  tail -f "$LOGFILE"
}

warmup() {
  ensure_venv
  load_env
  check_llama_vars
  note "Warming up llama.cpp once (loads model into OS cache) ..."
  time "$LLAMA_BIN" -m "$MODEL_PATH" -p "ready" -n 2 >/dev/null
  note "Warmup done."
}

debug_llm() {
  note "GET /debug/llm"
  curl -s "http://127.0.0.1:${PORT}/debug/llm" | jq . || true
}

test_chat() {
  note "GET /chat?q=hi"
  curl -s "http://127.0.0.1:${PORT}/chat?q=hi" | jq . || true
}

# --- main ---
cmd="${1:-help}"
case "$cmd" in
  start)     start_prod ;;
  dev)       start_dev ;;
  stop)      stop_srv ;;
  restart)   stop_srv; start_prod ;;
  status)    status_srv ;;
  logs)      logs_tail ;;
  warmup)    warmup ;;
  debug)     debug_llm ;;
  test)      test_chat ;;
  *)
    cat <<EOF
llm.sh — commands:
  start      Start server in background (nohup)
  dev        Start in foreground with --reload
  stop       Stop background server
  restart    Stop then start
  status     Show server status
  logs       Tail server logs
  warmup     Run one-time llama warmup (CLI)
  debug      Call /debug/llm (prints config)
  test       Call /chat?q=hi
Environment:
  HOST (default 0.0.0.0), PORT (default 8000)
  Reads .env for LLAMA_BIN, MODEL_PATH, etc.
EOF
    ;;
esac
