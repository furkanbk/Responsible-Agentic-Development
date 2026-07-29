"""tests.test_webhook — Phase 10: the GitHub webhook and the orphan watcher.

Owner: Alejandro Ramírez Trueba (HW3, T10.4).

No network and no model. The webhook's whole security property is testable as a
pure function — `verify_signature` — and its parsing as another, so neither test
opens a socket. The watcher is driven the way the TODO promises: "construct an
InboundEvent literal and assert what the webhook produced", against a controlled
temp tree and the isolated stores from `conftest.isolate_stores`.

What these protect, worst-first:
  1. an unsigned request cannot cause a single byte of work (verify_signature)
  2. a newly-orphaned decision is surfaced exactly once, by uid, never its text
  3. a push that orphaned nothing says nothing — and records why (silence branch)
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from channel.base import InboundEvent
from overlay import db as overlay_db
from triggers import orphan_watch, webhook

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- T10.1: signature verification -------------------------------------------

class TestVerifySignature:
    """The one gate between an internet stranger and the queue."""

    def test_correct_signature_accepted(self):
        body = b'{"ref": "refs/heads/main"}'
        assert webhook.verify_signature("s3cr3t", body, _sign("s3cr3t", body))

    def test_wrong_secret_rejected(self):
        body = b'{"ref": "refs/heads/main"}'
        assert not webhook.verify_signature("s3cr3t", body, _sign("other", body))

    def test_tampered_body_rejected(self):
        good = _sign("s3cr3t", b'{"ref": "refs/heads/main"}')
        assert not webhook.verify_signature("s3cr3t", b'{"ref": "refs/heads/evil"}', good)

    @pytest.mark.parametrize("header", [None, "", "sha1=deadbeef", "deadbeef"])
    def test_missing_or_wrong_algorithm_rejected(self, header):
        assert not webhook.verify_signature("s3cr3t", b"{}", header)

    def test_empty_secret_never_accepts(self):
        # A deployment with no secret must reject everything, not accept it.
        body = b"{}"
        assert not webhook.verify_signature("", body, _sign("", body))


# --- T10.1: parsing a delivery into an InboundEvent --------------------------

class TestParseEvent:
    def test_push_shape_and_dedupe_key(self, monkeypatch):
        monkeypatch.setenv("GITHUB_THREAD_KEY", "team")
        event = webhook.parse_event("push", _fixture("gh_push.json"))
        assert event is not None
        assert event.source == "github"
        assert event.external_user_id is None          # no human asked
        assert event.dedupe_key == "push:main"         # coalescable per branch
        assert event.coalescable is True
        assert event.interactive is False
        assert event.thread_key == "team"
        assert event.payload["changed_paths"] == ["pkg/new.py", "pkg/a.py", "pkg/gone.py"]
        assert event.payload["before"].startswith("aaaa")
        assert event.payload["after"].startswith("bbbb")

    def test_pull_request_dedupe_key(self):
        event = webhook.parse_event("pull_request", _fixture("gh_pull_request.json"))
        assert event is not None
        assert event.dedupe_key == "pr:7"
        assert event.payload["branch"] == "hw3/alejandro/webhook-orphan-watch"
        assert event.payload["changed_paths"] == []

    def test_unsupported_event_ignored(self):
        # Dozens of event types exist; anything but push/PR is dropped, not crashed.
        assert webhook.parse_event("issues", {"action": "opened"}) is None
        assert webhook.parse_event("push", "not-a-dict") is None

    def test_malformed_commits_do_not_raise(self):
        event = webhook.parse_event("push", {"ref": "refs/heads/x", "commits": [None, 5]})
        assert event is not None
        assert event.payload["changed_paths"] == []


# --- T10.2 / T10.3: the orphan watcher ---------------------------------------

def _write_tree(root: Path) -> None:
    """A tiny importable package: pkg, pkg.a (imports pkg.b), pkg.b. No pkg.gone."""
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("from pkg import b\n", encoding="utf-8")
    (pkg / "b.py").write_text("x = 1\n", encoding="utf-8")


@pytest.fixture
def repo_tree(tmp_path, monkeypatch):
    """A scannable tree plus RADF_SCAN_ROOT pointing the watcher at it."""
    root = tmp_path / "repo"
    root.mkdir()
    _write_tree(root)
    monkeypatch.setenv("RADF_SCAN_ROOT", str(root))
    return root


def _seed_decision(component: str) -> None:
    conn = overlay_db.connect()
    try:
        overlay_db.insert_decision(
            conn, component=component, decision=f"keep {component} as is",
            rationale="seeded for the orphan test", status="accepted",
            author_id="hw1",
        )
    finally:
        conn.close()


def _github_event() -> InboundEvent:
    payload = _fixture("gh_push.json")
    return InboundEvent(
        source="github", thread_key="team", dedupe_key="push:main",
        payload={
            "event": "push", "branch": "main",
            "changed_paths": ["pkg/new.py", "pkg/a.py", "pkg/gone.py"],
            "before": payload["before"], "after": payload["after"],
        },
    )


class TestOrphanWatch:
    def test_new_orphan_surfaced_once_by_uid(self, repo_tree):
        # A decision points at pkg/gone.py, which is absent from the tree; a
        # decision on pkg/a.py, which is present, must NOT be surfaced.
        _seed_decision("pkg/gone.py")
        _seed_decision("pkg/a.py")
        sent: list[tuple[str, str]] = []

        orphan_watch.handle_github_event(
            _github_event(), send=lambda tk, txt: sent.append((tk, txt))
        )

        assert len(sent) == 1                       # exactly one orphan surfaced
        thread, text = sent[0]
        assert thread == "team"
        assert "Module:pkg.gone" in text
        assert "Module:pkg.a" not in text           # the live decision stays quiet

    def test_orphan_message_leaks_no_decision_text(self, repo_tree):
        _seed_decision("pkg/gone.py")
        sent: list[str] = []
        orphan_watch.handle_github_event(
            _github_event(), send=lambda tk, txt: sent.append(txt)
        )
        # Names the uid and the commit range; never the decision's content.
        assert "keep pkg/gone.py as is" not in sent[0]
        assert "seeded for the orphan test" not in sent[0]
        assert "aaaaaaaaaa..bbbbbbbbbb" in sent[0]

    def test_second_pass_is_silent_via_watermark(self, repo_tree):
        # The orphan is new on the first pass, already-known on the second: the
        # count that decides comes from diffing two sets, not from the model.
        _seed_decision("pkg/gone.py")
        sent: list[str] = []
        send = lambda tk, txt: sent.append(txt)  # noqa: E731

        orphan_watch.handle_github_event(_github_event(), send=send)
        assert len(sent) == 1

        orphan_watch.handle_github_event(_github_event(), send=send)
        assert len(sent) == 1                       # nothing new -> nothing sent

        # The second pass recorded a silence; the first (a real finding) did not.
        conn = overlay_db.connect()
        try:
            silences = overlay_db.query_silences(conn, user_id=None)
        finally:
            conn.close()
        assert len(silences) == 1
        assert silences[0]["reason_code"] == "no_decisions_touched"

    def test_irrelevant_push_records_silence_and_sends_nothing(self, repo_tree):
        # No decision references anything the push touched, and nothing is
        # orphaned: a deliberate non-answer, written down (T10.3).
        _seed_decision("pkg/a.py")                  # live, not orphaned
        sent: list[str] = []
        payload = _fixture("gh_push_irrelevant.json")
        event = InboundEvent(
            source="github", thread_key="team", dedupe_key="push:main",
            payload={
                "event": "push", "branch": "main",
                "changed_paths": ["README.md"],
                "before": payload["before"], "after": payload["after"],
            },
        )

        orphan_watch.handle_github_event(event, send=lambda tk, txt: sent.append(txt))

        assert sent == []                           # nothing sent
        conn = overlay_db.connect()
        try:
            silences = overlay_db.query_silences(conn, user_id=None)
        finally:
            conn.close()
        assert len(silences) == 1
        row = silences[0]
        assert row["reason_code"] == "no_decisions_touched"
        assert row["trigger"] == "github"
        assert row["visibility"] == "team"
        assert "orphans_new=0" in row["evidence"]
        # Evidence is counts only — no decision text, no path content beyond counts.
        assert "keep " not in row["evidence"]
