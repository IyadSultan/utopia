#!/usr/bin/env bash
# Local KHCC pilot launcher: loopback-only Utopia on this machine.
# Usage: ./scripts/pilot-local.sh [start|stop|status|logs]
# Reads .env (RUST_LOG=info, restricted DB role, registration closed).
set -euo pipefail
cd "$(dirname "$0")/.."

PIDFILE=data/utopia-server.pid
LOG=data/utopia-server.log
BIN=target/release/utopia-server

case "${1:-start}" in
  start)
    [ -f .env ] || { echo ".env missing — see docs/utopia-pilot-security.md"; exit 1; }
    [ -x "$BIN" ] || { echo "build first: cargo build --release -p utopia-server"; exit 1; }
    colima status >/dev/null 2>&1 || colima start
    docker compose up -d db >/dev/null
    until docker compose exec -T db pg_isready -U utopia -d utopia >/dev/null 2>&1; do sleep 1; done
    mkdir -p data
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    nohup "$BIN" >>"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    for _ in $(seq 1 30); do
      curl -sf http://127.0.0.1:1516/api/v1/health >/dev/null 2>&1 && { echo "up: http://127.0.0.1:1516"; exit 0; }
      sleep 1
    done
    echo "server did not answer health within 30s — see $LOG"; exit 1 ;;
  stop)
    [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null && rm -f "$PIDFILE" && echo "server stopped"
    docker compose stop db >/dev/null && echo "db stopped" ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "server: running (pid $(cat "$PIDFILE"))"; curl -s http://127.0.0.1:1516/api/v1/health; echo
    else echo "server: stopped"; fi
    docker compose ps db ;;
  logs) tail -f "$LOG" ;;
  *) echo "usage: $0 [start|stop|status|logs]"; exit 1 ;;
esac
