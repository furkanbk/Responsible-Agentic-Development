#!/usr/bin/env bash
# app/bootstrap_demo.sh — get a fresh clone to the point where run_demo.sh works.
#
# Owner: Berat Furkan Kocak (final demo).
#
#   ./app/bootstrap_demo.sh          build whatever is missing, then verify
#   ./app/bootstrap_demo.sh --check  report only; build nothing; exit 1 if not ready
#   ./app/bootstrap_demo.sh --force  rebuild the summaries and the index anyway
#
# ## Why this exists
#
# `store/` is gitignored in full (.gitignore: store/*.json, store/*.db), so a
# fresh clone has NO derived graph and NO authored overlay. The corpus has three
# chunk kinds and only one of them survives that:
#
#     component   from node_summaries in the sqlite overlay   -> 0 on a clone
#     decision    from the overlay's decisions                -> 0 on a clone
#     doc         from CORPUS_DOCS, committed markdown        -> ~693 on a clone
#
# So `python -m retrieval.index` on a fresh clone *succeeds* and produces a
# doc-only index. Nothing errors. But every component card is missing, so a
# question like "which module holds the button that changes the meme" can only
# match prose in README.md and docs/ARCHITECTURE.md — which is wall-to-wall
# agentlib. It reads as a bad retriever; it is actually an empty corpus.
#
# That is also why the readiness check below asserts `component > 0` and that an
# `app.*` module is reachable, rather than `count(*) > 0` the way run_demo.sh
# does. A doc-only index passes a non-empty check, which is exactly the failure
# this script exists to catch.
#
# ## Order matters, and it is not obvious
#
#   scan -> summarize -> index
#
# `overlay.summarize` walks the derived graph, so a missing graph makes it a
# no-op rather than an error. `retrieval.index` chunks the summaries, so a missing
# overlay makes it doc-only rather than an error. Each step degrades quietly into
# the next one's silence, which is why they are sequenced here instead of left in
# the README for someone to run in whatever order.
#
# Everything here is idempotent: the scan rewrites derived data wholesale
# (CLAUDE.md §6), summarize skips any node whose content_sha is unchanged, and
# the index replaces itself. Re-running costs nothing but the checks.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
CONTAINER="radf-pgvector"
CHECK_ONLY=0
FORCE=0

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $arg (try --check, --force)" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[36m[bootstrap]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ok  \033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn \033[0m %s\n' "$*"; }
bad()  { printf '\033[31m fail \033[0m %s\n' "$*"; }
die()  { bad "$*"; exit 1; }

# In --check mode nothing is built; a missing piece is reported and the script
# keeps going so you get the WHOLE list of what is missing, not just the first.
MISSING=0
need() {  # need <description>  -> in check mode, record and skip the build
  if [ "$CHECK_ONLY" = 1 ]; then
    bad "$1"
    MISSING=$((MISSING + 1))
    return 1
  fi
  return 0
}

cd "$REPO_ROOT"

# --- 1. python + dependencies -------------------------------------------------
# run_demo.sh prefers .venv/bin/python and falls back to whatever python3 is on
# PATH. Creating the venv here keeps both scripts pointed at the same one, so a
# dependency installed now is a dependency run_demo.sh can see.
if [ ! -x "$VENV/bin/python" ]; then
  if need "no .venv — dependencies cannot be verified"; then
    say "creating .venv…"
    (command -v python3 || command -v python) >/dev/null || die "no python3 on PATH"
    "$(command -v python3 || command -v python)" -m venv "$VENV"
    ok "created $VENV"
  fi
fi
PY="$VENV/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

if ! "$PY" -c "import psycopg, openai, langgraph, dotenv" 2>/dev/null; then
  if need "python dependencies missing (psycopg / openai / langgraph / dotenv)"; then
    say "installing requirements.txt…"
    "$PY" -m pip install -q --upgrade pip
    "$PY" -m pip install -q -r "$REPO_ROOT/requirements.txt"
    ok "dependencies installed"
  fi
else
  ok "python deps present ($PY)"
fi

# --- 2. credentials -----------------------------------------------------------
# Two different providers, and they fail at different steps: OPENCODE_API_KEY is
# the agent and the summariser (step 5), OPENROUTER_API_KEY is the embeddings
# (step 6). A missing key surfaces halfway through a paid step otherwise.
[ -f "$REPO_ROOT/.env" ] || die ".env missing — copy .env.example to .env and fill it in"

env_has() {  # env_has KEY -> true if set to something non-empty in .env
  "$PY" - "$1" <<'PY'
import sys, pathlib
key = sys.argv[1]
for line in pathlib.Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line.startswith(f"{key}=") and line.split("=", 1)[1].strip().strip("'\""):
        sys.exit(0)
sys.exit(1)
PY
}

env_has OPENCODE_API_KEY   || die "OPENCODE_API_KEY is empty in .env — the agent and the summariser need it"
env_has OPENROUTER_API_KEY || die "OPENROUTER_API_KEY is empty in .env — the embeddings need it"
ok ".env has both API keys"
# Telegram is only the channel; the web app and search work without it, so this
# is a warning and not a stop. run_demo.sh will report the failed connect itself.
env_has TELEGRAM_BOT_TOKEN || warn "TELEGRAM_BOT_TOKEN empty — service.py will not connect (web demo still works)"

# --- 3. Postgres --------------------------------------------------------------
command -v docker >/dev/null || die "docker not found — the retrieval index needs the pgvector container"
if ! docker exec "$CONTAINER" pg_isready -U radf -d radf >/dev/null 2>&1; then
  if need "postgres container '$CONTAINER' not accepting connections"; then
    say "starting Postgres (docker compose)…"
    docker compose -f "$REPO_ROOT/docker-compose.yml" up -d >/dev/null
    for _ in $(seq 1 40); do
      docker exec "$CONTAINER" pg_isready -U radf -d radf >/dev/null 2>&1 && break
      sleep 1
    done
    docker exec "$CONTAINER" pg_isready -U radf -d radf >/dev/null 2>&1 \
      || die "postgres did not come up in 40s — 'docker compose logs postgres'"
    ok "postgres accepting connections on 127.0.0.1:5433"
  fi
else
  ok "postgres accepting connections on 127.0.0.1:5433"
fi

# --- 4. the derived graph -----------------------------------------------------
# scan_repository_structure has no CLI entrypoint — it is a tool the agent calls,
# not a script — hence the -c. kind="any" so markdown nodes land too; depth 5
# reaches every package in this tree.
graph_app_nodes() {
  "$PY" - <<'PY' 2>/dev/null || echo 0
import json, pathlib
p = pathlib.Path("store/knowledge_graph.json")
if not p.exists():
    print(0); raise SystemExit
g = json.loads(p.read_text())
nodes = g.get("nodes", [])
it = nodes if isinstance(nodes, list) else list(nodes.values())
ids = [(n.get("id") or n.get("name")) if isinstance(n, dict) else n for n in it]
print(sum(1 for i in ids if isinstance(i, str) and i.startswith("app")))
PY
}

if [ "$(graph_app_nodes)" -lt 2 ] || [ "$FORCE" = 1 ]; then
  if need "knowledge graph missing or has no app.* nodes"; then
    say "scanning the repository (derived graph)…"
    "$PY" -c "from tools.repo_scan import scan_repository_structure as s; print(s('.', 5, 'any'))"
    [ "$(graph_app_nodes)" -ge 2 ] || die "scan produced no app.* nodes — is the working directory the repo root?"
    ok "graph scanned — $(graph_app_nodes) app nodes"
  fi
else
  ok "graph present — $(graph_app_nodes) app nodes"
fi

# --- 5. authored node summaries ----------------------------------------------
# The expensive step: one cheap model call per node, ~88 nodes, idempotent on
# content_sha so a re-run after a demo costs nothing.
overlay_app_cards() {
  "$PY" - <<'PY' 2>/dev/null || echo 0
import pathlib, sqlite3
p = pathlib.Path("store/radf.db")
if not p.exists():
    print(0); raise SystemExit
try:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    print(c.execute(
        "SELECT count(*) FROM node_summaries WHERE symbol_uid LIKE 'Module:app%'"
    ).fetchone()[0])
except sqlite3.Error:
    print(0)
PY
}

if [ "$(overlay_app_cards)" -lt 1 ] || [ "$FORCE" = 1 ]; then
  if need "no node summaries for app.* in the overlay"; then
    say "summarising nodes (one cheap call each, a minute or two)…"
    if [ "$FORCE" = 1 ]; then
      "$PY" -m overlay.summarize --force
    else
      "$PY" -m overlay.summarize
    fi
    [ "$(overlay_app_cards)" -ge 1 ] || die "summarize wrote no app.* cards — check OPENCODE_API_KEY"
    ok "summaries written — $(overlay_app_cards) app cards"
  fi
else
  ok "summaries present — $(overlay_app_cards) app cards"
fi

# --- 6. the retrieval index ---------------------------------------------------
# component > 0 is the real check. run_demo.sh checks count(*) > 0, which a
# doc-only index passes — see the header.
index_counts() {
  "$PY" - <<'PY' 2>/dev/null || echo "{}"
from retrieval.store import connect, counts_by_kind
with connect() as c:
    print(counts_by_kind(c))
PY
}
component_count() {
  "$PY" - <<'PY' 2>/dev/null || echo 0
from retrieval.store import connect, counts_by_kind
with connect() as c:
    print(counts_by_kind(c).get("component", 0))
PY
}

if [ "$(component_count)" -lt 1 ] || [ "$FORCE" = 1 ]; then
  if need "retrieval index has no component chunks (doc-only or empty)"; then
    say "building the retrieval index (chunk, embed, load — ~40s, ~\$0.003)…"
    "$PY" -m retrieval.index
    [ "$(component_count)" -ge 1 ] || die "index still has no component chunks"
    ok "index built — $(index_counts)"
  fi
else
  ok "index populated — $(index_counts)"
fi

# --- 7. the acceptance test ---------------------------------------------------
# Not "is there data" but "does the demo's own question reach app.*". Everything
# above can pass while this fails — a stale index built before app/ existed would
# have component chunks and still answer with agentlib.
if [ "$CHECK_ONLY" = 1 ] && [ "$MISSING" -gt 0 ]; then
  echo
  bad "$MISSING step(s) not ready — run ./app/bootstrap_demo.sh (no --check) to build them"
  exit 1
fi

say "verifying: \"which module has the button that changes the meme\"…"
VERIFY="$("$PY" - <<'PY' 2>&1 || true
from retrieval.search import search
hits = search("which module has the button that changes the meme", k=5)
for h in hits:
    print(f"    {h.rank}. {h.chunk.source_path or h.chunk.heading_path}  [{h.chunk.kind}]")
print("APP_HIT" if any((h.chunk.source_path or "").startswith("app")
                       or (h.chunk.symbol_uid or "").startswith("Module:app")
                       for h in hits) else "NO_APP_HIT")
PY
)"
echo "$VERIFY" | grep -v '^APP_HIT$\|^NO_APP_HIT$' || true

if echo "$VERIFY" | grep -q '^APP_HIT$'; then
  ok "an app.* chunk is in the top 5"
  echo
  say "ready — now run: ./app/run_demo.sh"
else
  echo
  bad "no app.* chunk in the top 5 — the index does not cover the demo app"
  bad "try a full rebuild:  ./app/bootstrap_demo.sh --force"
  exit 1
fi
