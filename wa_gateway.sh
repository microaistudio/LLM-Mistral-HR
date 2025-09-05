\
#!/usr/bin/env bash
# Path: wa_gateway.sh
# Version: 3.1.1-wa
# Purpose: Manage WhatsApp gateway (LLM worker or Ask gateway).
# Notes: Headless by default; CRLF-safe (no heredocs).

set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$APP_DIR/logs"
LOG_FILE="$LOG_DIR/wa_gateway.out"
PID_FILE="$APP_DIR/.wa_gateway.pid"
APP_MODULE="${APP_MODULE:-whatsapp_llm_gateway:app}"
PORT="${PORT:-8011}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env.wa}"

# ---------- helpers ----------
choose_python() {
  if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    echo "$VIRTUAL_ENV/bin/python"; return
  fi
  if [[ -x "$APP_DIR/venv/bin/python" ]]; then
    echo "$APP_DIR/venv/bin/python"; return
  fi
  if command -v python3 >/dev/null 2>&1; then echo "python3"; return; fi
  if command -v python  >/dev/null 2>&1;  then echo "python";  return; fi
  echo "python"
}

ensure_uvicorn() {
  local py="$1"
  "$py" -c 'import uvicorn, fastapi, httpx' >/dev/null 2>&1 || return 1
}

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid; pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

banner() {
  echo "▶️  WhatsApp gateway"
  echo "    module: $APP_MODULE   port: $PORT"
  echo "    dir:    $APP_DIR"
  echo "    env:    ${ENV_FILE:-none}"
  echo "    LLM_API_URL=${LLM_API_URL:-unset}  ASK_HTTP_BASE=${ASK_HTTP_BASE:-unset}"
  echo "    WA_LLM_TIMEOUT_MS=${WA_LLM_TIMEOUT_MS:-unset}  WA_MAX_TOKENS=${WA_MAX_TOKENS:-unset}"
}

load_env() {
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
  fi
  export LLM_API_URL="${LLM_API_URL:-http://127.0.0.1:8000/chat}"
  export ASK_HTTP_BASE="${ASK_HTTP_BASE:-http://127.0.0.1:9000}"
  if [[ -n "${FAST_TOKENS:-}" && -z "${WA_MAX_TOKENS:-}" ]]; then
    export WA_MAX_TOKENS="$FAST_TOKENS"
  fi
  export WA_MAX_TOKENS="${WA_MAX_TOKENS:-128}"
}

kill_strays() {
  pkill -f "uvicorn .*${APP_MODULE}.*--port ${PORT}" >/dev/null 2>&1 || true
}

start_headless() {
  mkdir -p "$LOG_DIR"
  load_env

  local py; py="$(choose_python)"
  if ! ensure_uvicorn "$py"; then
    echo "❌ uvicorn/fastapi/httpx not found in $("$py" -c 'import sys; print(sys.executable)')"
    echo "   Run: $py -m pip install uvicorn fastapi httpx"
    exit 1
  fi

  banner
  if is_running; then
    echo "ℹ️  Already running (pid $(cat "$PID_FILE")). Use 'restart' if needed."
    exit 0
  fi

  kill_strays

  # Fully detach (headless), capture PID.
  if command -v setsid >/dev/null 2>&1; then
    setsid -f "$py" -m uvicorn --app-dir "$APP_DIR" "$APP_MODULE" \
      --host 0.0.0.0 --port "$PORT" --workers 1 --log-level info \
      >> "$LOG_FILE" 2>&1 < /dev/null || true
    sleep 0.5
    pgrep -f "uvicorn .*${APP_MODULE}.*--port ${PORT}" | head -n1 > "$PID_FILE" || true
  else
    nohup "$py" -m uvicorn --app-dir "$APP_DIR" "$APP_MODULE" \
      --host 0.0.0.0 --port "$PORT" --workers 1 --log-level info \
      >> "$LOG_FILE" 2>&1 < /dev/null &
    echo $! > "$PID_FILE"
  fi

  echo "✅ Started headless (pid $(cat "$PID_FILE" 2>/dev/null || echo '?')). Logs: $LOG_FILE"
}

start_attach() {
  start_headless
  echo "📜 Attaching logs (Ctrl+C to detach)"
  tail -n +1 -f "$LOG_FILE"
}

foreground() {
  load_env
  banner
  local py; py="$(choose_python)"
  exec "$py" -m uvicorn --app-dir "$APP_DIR" "$APP_MODULE" --host 0.0.0.0 --port "$PORT" --workers 1 --log-level info
}

stop() {
  if is_running; then
    local pid; pid="$(cat "$PID_FILE")"
    echo "⏹  Stopping pid $pid ..."
    kill "$pid" 2>/dev/null || true
    for _ in {1..20}; do
      sleep 0.2; is_running || break
    done
    if is_running; then
      echo "⚠️  Forcing stop ..."; kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "✅ Stopped."
  else
    kill_strays; echo "✅ Stopped (best-effort)."
  fi
}

logs()   { mkdir -p "$LOG_DIR"; echo "📜 Tailing $LOG_FILE  (Ctrl+C to exit)"; tail -n +1 -f "$LOG_FILE"; }
health() { curl -s "http://127.0.0.1:${PORT}/twilio/health" || true; echo; }
status() {
  if is_running; then
    echo "pid: $(cat "$PID_FILE")  port: $PORT  module: $APP_MODULE  log: $LOG_FILE"
  else
    ss -ltnp 2>/dev/null | grep ":$PORT " || echo "not running"
  fi
}

case "${1:-}" in
  start)          start_headless ;;
  start-attach)   start_attach ;;
  foreground)     foreground ;;
  stop)           stop ;;
  restart)        stop; start_headless ;;
  logs)           logs ;;
  health)         health ;;
  status)         status ;;
  *) echo "Usage: $0 {start|start-attach|foreground|stop|restart|logs|health|status}"; exit 1 ;;
esac
