"""tests/test_apply_change.py — the gated file write (HW2, T7.1d).

Owner: Alejandro Ramírez Trueba. New file (no existing owner).

`apply_change` is the one *irreversible* HW2 tool, so the interesting tests are
the refusals: every one must return a structured error branch AND leave the
target file byte-identical. The gate itself (the human y/N) is the loop's, not
the tool's — it is exercised in `tests/test_orchestration.py` and the T8.3 live
run; here we test what the gate cannot do, which is bound WHAT a write may touch.

The tool resolves paths against its module-level `_REPO_ROOT`, so every test
repoints that at a `tmp_path` sandbox: the confinement logic is identical, but a
bug can never scribble on the real repository.

Run:  python -m pytest tests/test_apply_change.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _online import online_key  # noqa: F401 — pytest fixture, used by name

import tools.apply_change as ac
from agentlib.loop import run_agent
from agentlib.schemas import schema_for
from agentlib.session import impact_scope


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A sandbox repo root with one editable file inside the impact set.

    Layout:
        pkg/mod.py        -> uid "Module:pkg.mod"   (in the impact set)
        pkg/other.py      -> uid "Module:pkg.other" (NOT in the impact set)
        .env              -> denylisted
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("original = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "other.py").write_text("other = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=shh\n", encoding="utf-8")
    monkeypatch.setattr(ac, "_REPO_ROOT", tmp_path.resolve())
    return tmp_path


IMPACT = ["Module:pkg.mod"]


# --- the write that is allowed to land ---------------------------------------


class TestApprovedWrite:
    def test_an_in_scope_edit_lands_and_reports_both_hashes(self, repo):
        target = repo / "pkg" / "mod.py"
        before = ac.sha256_of(target.read_text(encoding="utf-8"))

        with impact_scope(IMPACT):
            out = ac.apply_change("pkg/mod.py", "original = 2\n", intent="edit")

        assert out["written"] is True and out["intent"] == "edit"
        assert out["before_sha"] == before
        assert out["after_sha"] == ac.sha256_of("original = 2\n")
        assert out["bytes"] == len("original = 2\n".encode("utf-8"))
        assert target.read_text(encoding="utf-8") == "original = 2\n"

    def test_a_create_makes_a_new_in_scope_file_with_null_before_hash(self, repo):
        with impact_scope(["Module:pkg.new"]):
            out = ac.apply_change("pkg/new.py", "x = 1\n", intent="create")
        assert out["written"] is True and out["before_sha"] is None
        assert (repo / "pkg" / "new.py").read_text(encoding="utf-8") == "x = 1\n"


# --- every refusal leaves the file byte-identical ----------------------------


class TestConfinement:
    @pytest.mark.parametrize("path,expected", [
        ("../outside.py", "path_outside_scope"),        # traversal out of the repo
        ("pkg/../../outside.py", "path_outside_scope"),  # traversal that looks repo-relative
        (".env", "path_outside_scope"),                  # denylisted secret
        (".git/config", "path_outside_scope"),           # git internals
        ("store/radf.db", "path_outside_scope"),         # the agent's own overlay
        ("pkg/other.py", "outside_impact_set"),          # real file, not in the plan
    ])
    def test_out_of_scope_writes_are_refused_and_change_nothing(self, repo, path, expected):
        before = _snapshot(repo)
        with impact_scope(IMPACT):
            out = ac.apply_change(path, "MALICIOUS\n", intent="edit")
        assert out["error"] == expected
        assert _snapshot(repo) == before          # nothing on disk moved
        if expected == "outside_impact_set":
            assert out["impacted"] == IMPACT

    def test_no_impact_set_denies_every_write(self, repo):
        """Empty impact set is deny-all, not unrestricted."""
        before = _snapshot(repo)
        # No impact_scope in force.
        out = ac.apply_change("pkg/mod.py", "original = 2\n", intent="edit")
        assert out["error"] == "no_plan"
        assert _snapshot(repo) == before

    def test_confinement_is_checked_before_the_impact_set(self, repo):
        """A denylisted path is refused even under a permissive impact set."""
        with impact_scope(["Module:.env", "Module:pkg.mod"]):
            out = ac.apply_change(".env", "SECRET=leaked\n", intent="edit")
        assert out["error"] == "path_outside_scope"
        assert (repo / ".env").read_text(encoding="utf-8") == "SECRET=shh\n"


# --- intent guards -----------------------------------------------------------


class TestIntent:
    def test_edit_of_a_missing_file_is_file_missing(self, repo):
        with impact_scope(["Module:pkg.ghost"]):
            out = ac.apply_change("pkg/ghost.py", "x = 1\n", intent="edit")
        assert out["error"] == "file_missing"
        assert not (repo / "pkg" / "ghost.py").exists()

    def test_create_over_an_existing_file_is_file_exists(self, repo):
        with impact_scope(IMPACT):
            out = ac.apply_change("pkg/mod.py", "clobbered\n", intent="create")
        assert out["error"] == "file_exists"
        assert (repo / "pkg" / "mod.py").read_text(encoding="utf-8") == "original = 1\n"

    def test_a_bad_intent_is_invalid_args(self, repo):
        with impact_scope(IMPACT):
            out = ac.apply_change("pkg/mod.py", "x\n", intent="delete")  # type: ignore[arg-type]
        assert out["error"] == "invalid_args"


def _snapshot(root: Path) -> dict[str, str]:
    """Every file under `root`, path -> contents. The byte-identical witness."""
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8")
            for p in root.rglob("*") if p.is_file()}


# --- online: the real model reaching the gated tool (HW4, T13b / CLAUDE.md §8) -


@pytest.mark.online
def test_online_the_model_requests_the_gated_write_and_the_gate_declines(repo, online_key):
    """One real model call, ending at the gate with a refusal.

    Every other test here calls `apply_change` directly, which proves the
    confinement logic but says nothing about whether the tool is *reachable*:
    after the HW4 refactor the schema travels through `to_langchain_tool` and
    `bind_tools` before a model ever sees it, and a schema that silently fails to
    arrive is exactly the conversion bug §8 is aimed at. A direct call cannot
    catch it; only a live model asking for the tool by name can.

    Safe by two independent mechanisms, deliberately:
      * the `repo` fixture has already repointed `_REPO_ROOT` at a tmp sandbox,
        so even a landed write cannot touch the real repository;
      * the gate DECLINES, so the tool body never runs at all.

    The suite's byte-identical witness then holds for the same reason it does in
    the refusal tests above — a declined write is not a write.
    """
    before = _snapshot(repo)
    asked: list[tuple[str, dict]] = []

    def decline(name: str, args: dict) -> bool:
        asked.append((name, args))
        return False

    result = run_agent(
        "Use the apply_change tool to edit the file pkg/mod.py so its entire "
        "content is the single line: original = 2. Call the tool now.",
        schemas=[schema_for(ac.apply_change)],
        registry={"apply_change": ac.apply_change},
        approve=decline,
        verbose=False,
    )

    assert asked, ("the live model never requested apply_change — its schema did "
                   "not reach the model through the framework conversion")
    assert asked[0][0] == "apply_change"
    assert "path" in asked[0][1], f"model called the tool without a path: {asked[0][1]}"
    assert any(entry["branch"] == "declined" for entry in result["trace"])
    assert _snapshot(repo) == before, "a declined write must leave every file untouched"
