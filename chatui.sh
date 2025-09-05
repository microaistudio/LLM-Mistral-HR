#!/usr/bin/env bash
# chatui.sh — bring the chat UI up on :8000 (or down), no surprises.
set -Eeuo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

up() {
  ./llama-server.sh start
  ./llm.sh start
  echo
  echo "✅ Chat is up. Open: http://<VM-IP>:8000/static/chat.html"
}
down() {
  ./llm.sh stop
  ./llama-server.sh stop
}
status() {
  echo "---- llama-server (8081) ----"; ./llama-server.sh status || true
  echo "---- FastAPI (8000) ---------"; ./llm.sh status || true
}
logs() {
  echo "Tip: use these in two terminals:"
  echo "  ./llm.sh logs"
  echo "  ./llama-server.sh logs"
}
dev() {
  ./llama-server.sh start
  ./llm.sh dev
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  logs) logs ;;
  dev) dev ;;
  *) echo "Usage: $0 {up|down|status|logs|dev}"; exit 1 ;;
esac
