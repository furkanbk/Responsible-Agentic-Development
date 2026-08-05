"""app/reset_demo.py — put the demo back to its opening state.

Owner: Berat Furkan Kocak (final demo).

    python app/reset_demo.py            # reset, and say what changed
    python app/reset_demo.py --check    # report only, change nothing
    python app/reset_demo.py --reindex  # also rebuild the Postgres retrieval index

## What a demo run leaves behind

Two things, and only two:

  1. `app/theme.py` is edited by the `/change` beat (red -> blue -> maybe green).
  2. A decision lands on `Module:app.theme` in the sqlite overlay.

Resetting means undoing exactly those. Everything else the demo touched is
either derived and still correct, or authored and worth keeping.

## What reset does NOT touch, and why the app stays indexed

**The app is still indexed afterwards.** This is the question worth being precise
about, because "reset" sounds like it should cost the index and it does not:

  * **Graph nodes** (`app`, `app.server`, `app.theme`) live in
    `store/knowledge_graph.json` and are derived from the *tree*, not from file
    contents. Restoring a colour constant does not change which modules exist.
  * **Node summaries** live in the overlay's `node_summaries`, keyed on
    `symbol_uid` (#57), and describe what a module is *for*. "Holds the page's
    colours" is true whichever colour is in there.
  * **Retrieval chunks** in Postgres are built from those summaries, so they are
    equally unaffected. `app.theme` keeps both its cards.

`content_sha` is the one thing that could go stale — it is the sha of the source
when it was summarised, and the `/change` beat edits the source. Restoring
`theme.py` to its committed bytes restores the sha too, so staleness closes
itself. `--check` reports it either way rather than assuming.

The one case that needs `--reindex`: you recorded a decision AND rebuilt the
index during the demo, which would leave a chunk for a decision that no longer
exists. A plain demo run never reindexes, so this is rare — but a stale decision
chunk is exactly the kind of thing that is invisible until it answers a question
wrongly, so the flag exists.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
THEME = APP_DIR / "theme.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The component the demo edits and records against. Both reset steps key on it,
# so a demo aimed at a different module needs this one line changed.
DEMO_UID = "Module:app.theme"
DEMO_PATH = "app/theme.py"


def _say(ok: bool, text: str) -> None:
    print(f"  {'ok  ' if ok else '·   '} {text}")


def theme_is_pristine() -> tuple[bool, str]:
    """Does `app/theme.py` match what git has committed?

    Compared against `git show HEAD:` rather than against a hard-coded colour:
    the point is "back to the committed state", and a literal `#e23c3c` here
    would silently stop being the reset target the moment the demo's palette
    changed.
    """
    proc = subprocess.run(
        ["git", "show", f"HEAD:{DEMO_PATH}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, "not committed — cannot compare against HEAD"
    committed = proc.stdout
    current = THEME.read_text(encoding="utf-8")
    if current == committed:
        return True, "matches HEAD"
    return False, (f"differs from HEAD "
                   f"({_colour_of(current)} vs {_colour_of(committed)})")


def _colour_of(source: str) -> str:
    for line in source.splitlines():
        if line.startswith("BUTTON_COLOR"):
            return line.split("=", 1)[1].strip()
    return "?"


def restore_theme() -> bool:
    """`git checkout` the one file. Returns True if it actually changed."""
    pristine, _ = theme_is_pristine()
    if pristine:
        return False
    subprocess.run(["git", "checkout", "--", DEMO_PATH],
                   cwd=REPO_ROOT, check=True)
    return True


def demo_decisions() -> list[dict]:
    from overlay import db  # noqa: PLC0415 — after sys.path is set up

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT decision_id, decision, author_id, ts FROM decisions"
            " WHERE symbol_uid = ?", (DEMO_UID,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def drop_demo_decisions() -> int:
    """Delete the decisions the demo recorded on the demo component.

    Deliberately narrow: `WHERE symbol_uid = DEMO_UID`, never a bare
    `DELETE FROM decisions`. Decisions are the authored layer — the one thing no
    process may regenerate (CLAUDE.md §6) — so a reset script is exactly where a
    too-wide `DELETE` would do permanent damage, and it is scoped to the one
    component the demo is allowed to write to.
    """
    from overlay import db  # noqa: PLC0415

    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM decisions WHERE symbol_uid = ?", (DEMO_UID,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def summary_state() -> list[str]:
    """Whether `app.theme`'s summary cards still match the file on disk."""
    from overlay import db  # noqa: PLC0415

    conn = db.connect()
    try:
        rows = db.query_node_summaries(conn, symbol_uids=[DEMO_UID])
    finally:
        conn.close()
    if not rows:
        return [f"no summary cards for {DEMO_UID} — run: python -m overlay.summarize"]

    sha = hashlib.sha256(THEME.read_bytes()).hexdigest()
    stale = [r for r in rows if r["content_sha"] != sha]
    if stale:
        return [f"{len(rows)} summary card(s), {len(stale)} stale vs the file on disk"
                " — re-run: python -m overlay.summarize"]
    return [f"{len(rows)} summary card(s), all current"]


def index_state() -> list[str]:
    try:
        import psycopg  # noqa: PLC0415

        from retrieval.store import TABLE, dsn  # type: ignore
        with psycopg.connect(dsn(), connect_timeout=4) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {TABLE} WHERE symbol_uid = %s", (DEMO_UID,))
            mine = cur.fetchone()[0]
            cur.execute(f"SELECT count(*) FROM {TABLE}")
            total = cur.fetchone()[0]
            cur.execute(f"SELECT count(*) FROM {TABLE}"
                        " WHERE kind = 'decision' AND symbol_uid = %s", (DEMO_UID,))
            stale_decisions = cur.fetchone()[0]
        out = [f"{total} chunks indexed, {mine} for {DEMO_UID}"]
        if stale_decisions:
            out.append(f"WARNING: {stale_decisions} decision chunk(s) for a decision "
                       "that no longer exists — re-run with --reindex")
        return out
    except Exception as exc:  # noqa: BLE001 — a status line, never a crash
        return [f"index unreadable ({type(exc).__name__}) — is Postgres up?"]


def reindex() -> int:
    from dotenv import load_dotenv  # noqa: PLC0415

    from retrieval.index import main as index_main  # noqa: PLC0415

    load_dotenv()
    return index_main([])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reset the RADF demo to its opening state.")
    parser.add_argument("--check", action="store_true",
                        help="report only; change nothing")
    parser.add_argument("--reindex", action="store_true",
                        help="also rebuild the Postgres retrieval index")
    args = parser.parse_args(argv)

    print("demo state:")
    pristine, note = theme_is_pristine()
    _say(pristine, f"{DEMO_PATH}: {note}")
    decisions = demo_decisions()
    _say(not decisions, f"{DEMO_UID}: {len(decisions)} decision(s) on file")
    for row in decisions:
        print(f"        - {row['decision_id']}  {row['decision'][:64]}")
    for line in summary_state() + index_state():
        _say(True, line)

    if args.check:
        clean = pristine and not decisions
        print(f"\n{'already reset' if clean else 'NOT reset — run without --check'}")
        return 0 if clean else 1

    print("\nresetting:")
    changed = restore_theme()
    _say(True, f"{DEMO_PATH} restored from HEAD" if changed
               else f"{DEMO_PATH} already matched HEAD")
    dropped = drop_demo_decisions()
    _say(True, f"{dropped} decision(s) removed from {DEMO_UID}" if dropped
               else f"no decisions to remove from {DEMO_UID}")

    if args.reindex:
        print("\nrebuilding the retrieval index:")
        code = reindex()
        if code != 0:
            print("  index rebuild FAILED", file=sys.stderr)
            return code

    print("\nready. The graph, the summaries and the retrieval index are untouched —"
          "\napp.theme stays searchable; only the edit and the decision were undone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
