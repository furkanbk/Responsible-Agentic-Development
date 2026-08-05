#!/usr/bin/env bash
# app/run_demo.sh — bring the whole demo up: Postgres, the Telegram service, the web app.
#
# Owner: Berat Furkan Kocak (final demo).
#
#   ./app/run_demo.sh          start everything, tail nothing, print the URL
#   ./app/run_demo.sh stop     stop the two background jobs (leaves Postgres up)
#   ./app/run_demo.sh status   what is running right now
#
# Postgres is the ONE thing this script waits on rather than merely starting:
# `docker compose up -d` returns as soon as the container exists, but the
# retrieval index is unreadable for several seconds after that, and a demo whose
# first search returns `index_unavailable` looks like a broken retriever rather
# than a database that had not finished booting. So: start, then poll pg_isready.
#
# service.py runs with `python -u`. Without it, Python block-buffers stdout the
# moment it is a pipe instead of a terminal, and the log panel sits empty for
# minutes and then dumps everything at once — which reads exactly like a hung
# agent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_ROOT/app"
LOG_DIR="$APP_DIR/logs"
SERVICE_LOG="$LOG_DIR/service.log"
APP_LOG="$LOG_DIR/app.log"
SERVICE_PID="$LOG_DIR/service.pid"
APP_PID="$LOG_DIR/app.pid"
PORT="${PORT:-8080}"
CONTAINER="radf-pgvector"
# service.py already defaults to strong; passed explicitly so the demo's model
# is visible in `ps` and in this file, and so `MODEL=cheap ./app/run_demo.sh`
# is a one-word change rather than an edit.
MODEL="${MODEL:-strong}"

mkdir -p "$LOG_DIR"

say()  { printf '\033[36m[demo]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ok  \033[0m %s\n' "$*"; }
bad()  { printf '\033[31m fail \033[0m %s\n' "$*"; }

python_bin() {
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    echo "$REPO_ROOT/.venv/bin/python"
  else
    command -v python3 || command -v python
  fi
}

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

stop_jobs() {
  for pidfile in "$APP_PID" "$SERVICE_PID"; do
    if alive "$pidfile"; then
      pid="$(cat "$pidfile")"
      kill "$pid" 2>/dev/null || true
      say "stopped pid $pid ($(basename "$pidfile" .pid))"
    fi
    rm -f "$pidfile"
  done
}

case "${1:-start}" in
  stop)
    stop_jobs
    say "Postgres left running — 'docker compose down' if you want it gone too."
    exit 0
    ;;
  status)
    alive "$SERVICE_PID" && ok "service.py  pid $(cat "$SERVICE_PID")" || bad "service.py not running"
    alive "$APP_PID"     && ok "web app     pid $(cat "$APP_PID") — http://127.0.0.1:$PORT" \
                         || bad "web app not running"
    docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
      && ok "postgres    $CONTAINER" || bad "postgres container not running"
    exit 0
    ;;
esac

PY="$(python_bin)"
cd "$REPO_ROOT"

# --- 0. anything already up from a previous run goes first --------------------
stop_jobs

# --- 1. Postgres + pgvector ---------------------------------------------------
say "starting Postgres (docker compose)…"
docker compose -f "$REPO_ROOT/docker-compose.yml" up -d >/dev/null
for _ in $(seq 1 40); do
  if docker exec "$CONTAINER" pg_isready -U radf -d radf >/dev/null 2>&1; then
    ok "postgres accepting connections on 127.0.0.1:5433"
    break
  fi
  sleep 1
done
if ! docker exec "$CONTAINER" pg_isready -U radf -d radf >/dev/null 2>&1; then
  bad "postgres did not come up in 40s — 'docker compose logs postgres'"
  exit 1
fi

# The index is DERIVED (CLAUDE.md §6): an empty one is a rebuild away, never a
# reason to stop. Reported, not fixed — a rebuild costs a minute and some
# embedding calls, and doing that silently inside a launcher is a surprise.
CHUNKS="$("$PY" - <<'PY' 2>/dev/null || true
import psycopg
from retrieval.store import TABLE, dsn
with psycopg.connect(dsn(), connect_timeout=5) as c, c.cursor() as cur:
    cur.execute(f"SELECT count(*) FROM {TABLE}")
    print(cur.fetchone()[0])
PY
)"
if [ "${CHUNKS:-0}" -gt 0 ] 2>/dev/null; then
  ok "retrieval index populated — $CHUNKS chunks"
else
  bad "retrieval index empty or unreadable — run:  $PY -m retrieval.index --rebuild"
fi

# --- 2. the Telegram service --------------------------------------------------
say "starting service.py (telegram, --model $MODEL)…"
: > "$SERVICE_LOG"
"$PY" -u "$REPO_ROOT/service.py" --model "$MODEL" >>"$SERVICE_LOG" 2>&1 &
echo $! > "$SERVICE_PID"
sleep 3
if alive "$SERVICE_PID" && grep -q "\[service\] connected as" "$SERVICE_LOG"; then
  ok "$(grep -m1 '\[service\] connected as' "$SERVICE_LOG")"
else
  bad "service.py did not connect — tail of its log:"
  tail -n 8 "$SERVICE_LOG" | sed 's/^/       /'
fi

# --- 3. the web app -----------------------------------------------------------
say "starting the web app…"
"$PY" -u "$APP_DIR/server.py" --port "$PORT" >>"$APP_LOG" 2>&1 &
echo $! > "$APP_PID"
sleep 1
if alive "$APP_PID"; then
  ok "http://127.0.0.1:$PORT"
else
  bad "web app failed to start:"
  tail -n 8 "$APP_LOG" | sed 's/^/       /'
  exit 1
fi

echo
say "up. logs: $SERVICE_LOG  ·  $APP_LOG"
say "stop with: ./app/run_demo.sh stop"
