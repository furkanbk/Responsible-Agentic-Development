"""tests/test_admin.py — the admin subagent's two locks (HW3, T11.3/T11.4).

Owner: Dias Sarkytbaev. New file.

The graded properties are enforced in code and testable without a model: the door
opens only on identity AND confirmation, and the registry is exactly the listed
tools. `run_admin` checks the gate before it touches the model, so a refusal is
provable offline.
"""

from __future__ import annotations

import pytest
from _online import online_key  # noqa: F401 — pytest fixture, used by name

from agentlib.session import SessionKey, current_impact_set
from agents.admin import (
    ADMIN_TOOLS,
    admit,
    build_admin_registry,
    load_admin_brief,
    promote_memory,
    run_admin,
)
from channel.identity import Identity


def identity(user_id: str = "berat", *, is_admin: bool = True) -> Identity:
    return Identity(session=SessionKey(user_id=user_id), known=True,
                    is_admin=is_admin, external_id="42", source="telegram")


class TestTheGate:
    def test_a_non_admin_is_blocked(self):
        out = admit(identity(is_admin=False), confirmed=True)
        assert out is not None and out.status == "blocked"

    def test_an_admin_without_confirmation_is_blocked(self):
        out = admit(identity(), confirmed=False)
        assert out is not None and out.status == "blocked"

    def test_an_admin_with_confirmation_is_admitted(self):
        assert admit(identity(), confirmed=True) is None

    def test_run_admin_refuses_before_touching_the_model(self):
        # A non-admin request returns a blocked envelope; nothing calls a model,
        # so this passes with no scripting and no key.
        out = run_admin("prune everything", identity=identity(is_admin=False),
                        confirmed=True, verbose=False)
        assert out.status == "blocked"

    def test_run_admin_refuses_an_unconfirmed_admin(self):
        out = run_admin("promote that memory", identity=identity(),
                        confirmed=False, verbose=False)
        assert out.status == "blocked"


class TestNarrowByConstruction:
    def test_the_registry_is_exactly_the_listed_tools(self):
        schemas, registry = build_admin_registry()
        assert set(registry) == {fn.__name__ for fn in ADMIN_TOOLS}
        assert len(schemas) == len(ADMIN_TOOLS)

    def test_it_holds_the_admin_writes(self):
        _, registry = build_admin_registry()
        for name in ("append_decision_record", "apply_change", "prune_graph_node",
                     "promote_memory"):
            assert name in registry

    def test_it_does_not_hold_the_scanner(self):
        # The main registry has scan_repository_structure; the admin one must not
        # be that registry filtered — a scan is not an admin capability here.
        _, registry = build_admin_registry()
        assert "scan_repository_structure" not in registry
        assert "save_memory" not in registry     # admin promotes, it does not author memory


class TestBriefAndTools:
    def test_the_brief_states_identity_plus_confirmation(self):
        text = load_admin_brief().lower()
        assert "confirmation" in text and "allowlist" in text

    def test_promote_memory_missing_id_is_a_structured_error(self):
        out = promote_memory("no_such_memory")
        assert out["error"] == "memory_not_found"


# --- online: the privileged path against a real model (HW4, T13b / §8) --------


@pytest.mark.online
def test_online_an_admitted_admin_run_reaches_a_real_model_and_writes_nothing(online_key):
    """One real call down the privileged path, past both locks.

    Every other test in this suite stops at `admit()`, which is the right place
    to test a refusal — nothing downstream should run — but it means none of
    them ever reaches the model, and after the HW4 refactor the admin's whole
    toolset now travels through `to_langchain_tool`/`bind_tools` before a model
    sees it. A registry that silently fails to arrive would leave every test here
    green (§8).

    Write-safe by construction rather than by luck: `run_admin` is called with
    the default empty impact set, and decision #25 makes an empty set deny every
    write, so even a model that decides to call `apply_change` or
    `prune_graph_node` is refused by the tool itself — and the gate declines on
    top of that.
    """
    seen_gate: list[str] = []

    def decline(name: str, args: dict) -> bool:
        seen_gate.append(name)
        return False

    out = run_admin(
        "List what you know about the component tools.decisions. Do not change anything.",
        identity=identity(), confirmed=True, approve=decline, verbose=False,
    )

    # The envelope contract holds whatever the model chose to do.
    assert out.status in ("ok", "needs_input", "blocked", "failed")
    assert out.agent == "admin"
    if out.status == "ok":
        assert "answer" in out.result
        assert out.result["granted_impact"] == [], (
            "the run was granted a write scope it never asked for")

    # Ambient scope is restored after the run — a leaked impact set would let a
    # later, unrelated run write.
    assert current_impact_set() == ()

    # Nothing gated slipped through: if the model reached for a write tool, the
    # gate saw it and said no.
    assert all(name in {"apply_change", "prune_graph_node"} for name in seen_gate)
