"""tests.test_overlay — the authored layer (HW2, T4.6).

Owner: Berat Furkan Kocak.

Run:  python -m pytest tests/test_overlay.py -v

Fully offline: no model calls. The properties under test are the ones the whole
HW2 design rests on —

  * the derived/authored separation now holds ACROSS TWO STORES: a rescan
    rebuilds the JSON graph wholesale and the overlay is untouched;
  * `symbol_uid` normalises every spelling of a component to one key, which is
    what makes the later GitNexus swap a remap (ARCHITECTURE.md §6.1);
  * scoping is enforced in the query, so A's private rows cannot reach B.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from overlay import db as db_mod
from overlay import memory as mem_mod
from overlay.uid import resolve_uid
from tools.repo_scan import scan_repository_structure


@pytest.fixture()
def overlay_env(tmp_path, monkeypatch):
    """Point every store at tmp_path. Env is read at call time (decision #11)."""
    monkeypatch.setenv("RADF_DB_PATH", str(tmp_path / "radf.db"))
    monkeypatch.setenv("RADF_MEMORY_PATH", str(tmp_path / "memory.json"))
    monkeypatch.setenv("RADF_GRAPH_PATH", str(tmp_path / "knowledge_graph.json"))
    return tmp_path


@pytest.fixture()
def sample_repo(tmp_path):
    """A tiny synthetic tree so a scan has something real to walk."""
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text("import pkg.b\n\ndef alpha():\n    pass\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("def beta():\n    pass\n", encoding="utf-8")
    return root


class TestResolveUid:
    def test_every_spelling_collapses_to_one_key(self):
        # The three ways HW1 code refers to the same module.
        assert resolve_uid("agentlib.core") == "Module:agentlib.core"
        assert resolve_uid("agentlib/core.py") == "Module:agentlib.core"
        assert resolve_uid("agentlib\\core.py") == "Module:agentlib.core"

    def test_package_init_collapses_to_package(self):
        # Matches repo_scan's node-id convention (decision #17).
        assert resolve_uid("tools/__init__.py") == "Module:tools"

    def test_markdown_keeps_its_path(self):
        assert resolve_uid("docs/ARCHITECTURE.md") == "Doc:docs/ARCHITECTURE.md"

    def test_idempotent(self):
        once = resolve_uid("agentlib/core.py")
        assert resolve_uid(once) == once

    def test_repo_wide_is_none_not_an_error(self):
        # A decision with no component applies everywhere. That is a real row.
        assert resolve_uid(None) is None
        assert resolve_uid("   ") is None


class TestDerivedAuthoredSeparation:
    def test_rescan_rebuilds_structure_and_leaves_the_overlay_untouched(
        self, overlay_env, sample_repo
    ):
        """The HW1 invariant (CLAUDE.md §6), now across two stores."""
        conn = db_mod.connect()
        db_mod.insert_decision(
            conn,
            component="pkg.a",
            decision="alpha() stays synchronous",
            rationale="callers are sync",
            status="accepted",
            author_id="berat",
        )
        overlay_file = db_mod.db_path()
        before = overlay_file.read_bytes()

        # The tool reports counts, not the nodes themselves.
        first = scan_repository_structure(str(sample_repo), 5, "python")
        assert first["nodes"] > 0, "scan produced no nodes"

        # Mutate the tree and rescan: structure must change, overlay must not.
        (sample_repo / "pkg" / "c.py").write_text("def gamma():\n    pass\n", encoding="utf-8")
        second = scan_repository_structure(str(sample_repo), 5, "python")

        assert second["nodes"] > first["nodes"]
        assert overlay_file.read_bytes() == before, (
            "a rescan modified the authored overlay — the derived layer must "
            "never be able to reach it"
        )

    def test_decision_survives_a_scan_that_wipes_structure(self, overlay_env, sample_repo):
        conn = db_mod.connect()
        db_mod.insert_decision(
            conn, component="pkg.a", decision="keep it", rationale="because",
            status="accepted", author_id="berat",
        )
        scan_repository_structure(str(sample_repo), 5, "python")

        conn2 = db_mod.connect()
        rows = db_mod.query_decisions(conn2, user_id="berat")
        assert [r["decision"] for r in rows] == ["keep it"]


class TestDecisionScoping:
    def _seed(self, conn):
        db_mod.insert_decision(
            conn, component="tools/decisions.py",
            decision="Keep the _-private I/O helpers where they are",
            rationale="Phase 1 imports them; lifting is a contract change",
            status="accepted", author_id="alejandro", visibility="team",
        )
        db_mod.insert_decision(
            conn, component="tools/decisions.py",
            decision="Berat: prefer explicit returns in this module",
            rationale="personal taste", status="accepted",
            author_id="berat", visibility="user:berat",
        )
        db_mod.insert_decision(
            conn, component=None, decision="Raw stdlib only, no frameworks",
            rationale="CLAUDE.md §4", status="accepted",
            author_id="berat", visibility="team",
        )

    def test_private_stays_private(self, overlay_env):
        conn = db_mod.connect()
        self._seed(conn)
        uid = resolve_uid("tools/decisions.py")

        dias = [d["decision"] for d in db_mod.query_decisions(conn, user_id="dias", symbol_uids=[uid])]
        assert not any("Berat:" in d for d in dias), (
            "another user's private decision reached dias"
        )

    def test_shared_reaches_everyone(self, overlay_env):
        conn = db_mod.connect()
        self._seed(conn)
        uid = resolve_uid("tools/decisions.py")

        for who in ("berat", "dias", "alejandro"):
            seen = [d["decision"] for d in db_mod.query_decisions(conn, user_id=who, symbol_uids=[uid])]
            assert any("_-private I/O helpers" in d for d in seen), f"{who} missed the team decision"

    def test_repo_wide_decisions_survive_a_narrowed_query(self, overlay_env):
        conn = db_mod.connect()
        self._seed(conn)
        seen = [
            d["decision"]
            for d in db_mod.query_decisions(conn, user_id="dias", symbol_uids=[resolve_uid("tools/decisions.py")])
        ]
        assert any("Raw stdlib only" in d for d in seen), (
            "narrowing to an impact set dropped the repo-wide constraints"
        )

    def test_component_spelling_does_not_change_what_is_found(self, overlay_env):
        conn = db_mod.connect()
        self._seed(conn)
        by_path = db_mod.query_decisions(conn, user_id="dias", symbol_uids=[resolve_uid("tools/decisions.py")])
        by_dotted = db_mod.query_decisions(conn, user_id="dias", symbol_uids=[resolve_uid("tools.decisions")])
        assert [d["decision_id"] for d in by_path] == [d["decision_id"] for d in by_dotted]

    def test_unauthenticated_read_sees_team_only(self, overlay_env):
        conn = db_mod.connect()
        self._seed(conn)
        seen = db_mod.query_decisions(conn, user_id=None)
        assert all(d["visibility"] == "team" for d in seen)


class TestLegacyImport:
    def test_import_is_idempotent(self, overlay_env):
        conn = db_mod.connect()
        graph = {"decisions": [
            {"component": "agentlib.core", "decision": "Zen wrapper",
             "rationale": "HW1", "status": "accepted", "ts": "2026-07-23"},
        ]}
        assert db_mod.import_legacy_decisions(conn, graph) == 1
        assert db_mod.import_legacy_decisions(conn, graph) == 0
        assert len(db_mod.query_decisions(conn, user_id="berat")) == 1

    def test_imported_rows_are_team_visible(self, overlay_env):
        conn = db_mod.connect()
        db_mod.import_legacy_decisions(conn, {"decisions": [
            {"component": "agentlib.core", "decision": "Zen wrapper",
             "rationale": "HW1", "status": "accepted"},
        ]})
        row = db_mod.query_decisions(conn, user_id="dias")[0]
        assert row["visibility"] == "team" and row["author_id"] == "hw1"
        assert row["symbol_uid"] == "Module:agentlib.core"


class TestMemoryScoping:
    def test_private_fact_never_surfaces_for_another_user(self, overlay_env):
        mem_mod.save_memory(
            "Pays for Netflix and HBO", cue=["subscription", "watch"],
            visibility="user:alice", author="alice", stated=True,
        )
        assert mem_mod.retrieve_memory("where can I watch it", user_id="alice")
        assert mem_mod.retrieve_memory("where can I watch it", user_id="bob") == []

    def test_team_memory_reaches_everyone(self, overlay_env):
        mem_mod.save_memory(
            "The scan ignores .venv by convention", cue=["scan", "venv"],
            visibility="team", author="alejandro", stated=True,
        )
        assert mem_mod.retrieve_memory("scan", user_id="bob")


class TestSaveDiscipline:
    def test_inferred_memory_needs_a_second_observation(self, overlay_env):
        first = mem_mod.save_memory(
            "Prefers pytest fixtures over unittest setUp", cue=["pytest"],
            visibility="user:berat", author="berat",
        )
        assert first["status"] == "proposed"
        # Not yet allowed to shape behaviour.
        assert mem_mod.retrieve_memory("pytest", user_id="berat") == []

        second = mem_mod.save_memory(
            "Prefers pytest fixtures over unittest setUp", cue=["fixture"],
            visibility="user:berat", author="berat",
        )
        assert second["status"] == "accepted"
        assert second["promoted_by"] == "second_observation"
        assert mem_mod.retrieve_memory("pytest", user_id="berat")

    def test_a_stated_preference_is_accepted_immediately(self, overlay_env):
        row = mem_mod.save_memory(
            "Always run the tests before proposing a diff", cue=["test"],
            visibility="user:berat", author="berat", stated=True,
        )
        assert row["status"] == "accepted"

    def test_saving_the_same_text_twice_does_not_duplicate(self, overlay_env):
        mem_mod.save_memory("one thing", cue=["a"], visibility="team", author="x", stated=True)
        mem_mod.save_memory("one thing", cue=["b"], visibility="team", author="x", stated=True)
        rows = mem_mod.all_memories("x")
        assert len(rows) == 1
        assert rows[0]["cue"] == ["a", "b"], "cues should merge, not fork the record"

    def test_every_memory_carries_its_source_and_is_marked_quoted(self, overlay_env):
        row = mem_mod.save_memory("something", visibility="team", author="alice", stated=True)
        # This flag is what lets the renderer quote it instead of obeying it.
        assert row["source"] == {"author": "alice", "session_id": "", "quoted": True}


class TestRetrievalRanking:
    def test_cue_match_beats_an_unrelated_memory(self, overlay_env):
        mem_mod.save_memory("uses zsh", cue=["shell"], visibility="team", author="x", stated=True)
        mem_mod.save_memory("owns the loop", cue=["loop", "agentlib"], visibility="team",
                            author="x", stated=True)
        top = mem_mod.retrieve_memory("who owns agentlib?", user_id="x")[0]
        assert top["text"] == "owns the loop"

    def test_applies_to_binds_via_the_impact_set(self, overlay_env):
        """The graph routes: a bound memory surfaces on impact, not on wording."""
        mem_mod.save_memory(
            "this module returns dicts, never raises", cue=[],
            applies_to="Module:tools.decisions", visibility="team",
            author="dias", stated=True,
        )
        # Query text mentions none of it; the impact set does the work.
        hits = mem_mod.retrieve_memory(
            "change something", user_id="dias", applies_to=["Module:tools.decisions"]
        )
        assert hits and hits[0]["text"].startswith("this module returns dicts")

    def test_an_unbound_rule_is_always_a_candidate(self, overlay_env):
        mem_mod.save_memory("keep replies short", kind="rule", visibility="team",
                            author="x", stated=True)
        assert mem_mod.retrieve_memory("anything at all", user_id="x")


class TestScratchTraceability:
    def test_writes_are_append_only(self, overlay_env):
        conn = db_mod.connect()
        rid = db_mod.start_run(conn, user_id="berat", thread_id="t", agent="orchestrator")
        db_mod.scratch_write(conn, run_id=rid, agent="planner", step=1, key="plan", value={"v": 1})
        db_mod.scratch_write(conn, run_id=rid, agent="planner", step=2, key="plan", value={"v": 2})

        writes = db_mod.scratch_dump(conn, rid)["writes"]
        assert len(writes) == 2, "a second write to the same key overwrote the first"
        # The reader gets the latest, but the earlier value is still on the record.
        assert db_mod.scratch_read(conn, run_id=rid, agent="executor", step=1, key="plan") == {"v": 2}
        assert json.loads(writes[0]["value"]) == {"v": 1}

    def test_a_missed_read_is_logged(self, overlay_env):
        """'The executor looked and found nothing' is the trace worth having."""
        conn = db_mod.connect()
        rid = db_mod.start_run(conn, user_id="berat", thread_id="t", agent="orchestrator")
        assert db_mod.scratch_read(conn, run_id=rid, agent="executor", step=1, key="plan") is None

        reads = db_mod.scratch_dump(conn, rid)["reads"]
        assert len(reads) == 1 and reads[0]["saw_seq"] is None

    def test_read_set_pins_which_write_was_observed(self, overlay_env):
        conn = db_mod.connect()
        rid = db_mod.start_run(conn, user_id="berat", thread_id="t", agent="orchestrator")
        seq1 = db_mod.scratch_write(conn, run_id=rid, agent="planner", step=1, key="plan", value="a")
        db_mod.scratch_read(conn, run_id=rid, agent="executor", step=1, key="plan")
        seq2 = db_mod.scratch_write(conn, run_id=rid, agent="planner", step=2, key="plan", value="b")
        db_mod.scratch_read(conn, run_id=rid, agent="executor", step=2, key="plan")

        saw = [r["saw_seq"] for r in db_mod.scratch_dump(conn, rid)["reads"]]
        assert saw == [seq1, seq2], "the causal chain is not replayable"


class TestCorruptStore:
    def test_an_unreadable_memory_file_is_refused_not_recreated(self, overlay_env):
        Path(mem_mod.memory_path()).parent.mkdir(parents=True, exist_ok=True)
        mem_mod.memory_path().write_text("{not json", encoding="utf-8")
        before = mem_mod.memory_path().read_bytes()

        with pytest.raises(mem_mod.MemoryStoreCorrupt):
            mem_mod.retrieve_memory("anything", user_id="berat")

        assert mem_mod.memory_path().read_bytes() == before, (
            "a corrupt store was rewritten — authored memory would be lost "
            "(the rule behind decision #12)"
        )
