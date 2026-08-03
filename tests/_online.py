"""tests/_online.py — the shared gate for online tests (CLAUDE.md §8).

Owner: Dias Sarkytbaev (HW4, Phase 13b). Not a test module (no `test_` prefix,
so pytest does not collect it) — it is the one place the "can this suite make a
real call?" question is answered.

§8 requires every framework-touching suite to carry at least one test that makes
a real call, marked so an environment without a key can skip it. Three details
are easy to get wrong, and centralising them means four suites cannot each get
them wrong in a different way:

* **The key lives in `.env`, not the shell environment.** Check only
  `os.environ` and the online tests skip on precisely the machine that has a
  key — and §8 is explicit that they must actually run somewhere before merge.

* **A placeholder is not a key.** `.env.example` ships `OPENCODE_API_KEY=sk-...`
  and any half-configured checkout copies it verbatim. That string is truthy, so
  a plain `os.environ.get(...)` check lets the test run and then reports the
  provider's 401 as a *test failure*. A missing key is a SKIP: the suite has
  nothing to say about correctness when it cannot reach a model, and only a real
  failure should be red.

* **Reading `.env` must not EXPORT it.** A module-level `load_dotenv()` here puts
  the placeholder into `os.environ` for the whole session, which silently flips
  the skip decision of every other suite that checks `os.environ` — including
  `test_graph_agent.py`, whose online test then runs and 401s. So the decision
  reads `.env` without mutating anything (`dotenv_values`), and the real key is
  exported only for the duration of the one test that asked for it, by fixture.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest
from dotenv import dotenv_values

_KEY = "OPENCODE_API_KEY"

#: Values that mean "nobody filled this in", not "here is a key".
_PLACEHOLDERS = frozenset({"", "sk-...", "sk-xxx", "changeme", "your-key-here"})


def configured_key() -> str:
    """The real key from the environment or `.env`, or "" if there isn't one.

    Reads `.env` with `dotenv_values`, which returns a dict and leaves
    `os.environ` untouched — see the module docstring for why that matters.
    """
    key = (os.environ.get(_KEY) or dotenv_values().get(_KEY) or "").strip()
    if key in _PLACEHOLDERS or key.endswith("..."):
        return ""
    return key


def have_real_key() -> bool:
    return bool(configured_key())


@pytest.fixture
def online_key() -> Iterator[str]:
    """Skip unless a real key exists; export it for this test only.

    Import this into a suite and take it as an argument on the online test:

        from _online import online_key  # noqa: F401  (pytest fixture)

        @pytest.mark.online
        def test_online_something(online_key):
            ...

    `agentlib.core` reads the key from `os.environ` at call time, so it has to be
    exported — but only around the test that needs it, and restored afterwards,
    so no other suite's skip decision changes underneath it.
    """
    key = configured_key()
    if not key:
        pytest.skip(f"{_KEY} is unset or still a placeholder — online test skipped")

    previous = os.environ.get(_KEY)
    os.environ[_KEY] = key
    try:
        yield key
    finally:
        if previous is None:
            os.environ.pop(_KEY, None)
        else:
            os.environ[_KEY] = previous
