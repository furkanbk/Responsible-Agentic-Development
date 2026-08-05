"""app.theme — the demo page's look, as data.

Owner: Berat Furkan Kocak (final demo).

One module holding the handful of values that decide what the page looks like.
It exists so the *appearance* of the app is a thing the agent can be asked to
change: `repo_scan` indexes `.py` and `.md` only, so a colour living in
`static/index.html` is not a graph node, is not in the retrieval corpus, and is
outside any plan's impact set — `apply_change` would refuse it with
`outside_impact_set` no matter how well the change was planned.

Moving the colour into a module is not a workaround for that rule. It is the
rule working: the graph is what the system can reason about, and anything it
should be able to change has to be somewhere it can see. A CSS file it cannot
index is a part of the app the agent has no business editing blind.

`server.py` substitutes these into the page at request time, so a change here
shows up on the next reload — no build step, no restart.
"""

from __future__ import annotations

# The big round button. The demo's "change the colour" request lands here.
BUTTON_COLOR = "#e23c3c"

# Its lit edge and its shadow. Derived by hand rather than computed so that a
# request to change the button's colour touches one obvious value and the page
# still looks deliberate afterwards.
BUTTON_HIGHLIGHT = "#ff5252"
BUTTON_SHADOW = "#8f1d1d"

# What the caption under the button says.
BUTTON_CAPTION = "click to change the meme"

# The product name in the header.
BRAND_NAME = "Billion Dollar Startup"
BRAND_TAGLINE = "funny memes as a service"


def as_substitutions() -> dict[str, str]:
    """The `{{TOKEN}} -> value` map `server.py` applies to `static/index.html`.

    Keyed by token rather than by variable name so a rename here cannot silently
    stop substituting: an unknown token stays visible in the page as
    `{{WHATEVER}}`, which is a loud failure instead of a blank style.
    """
    return {
        "{{BUTTON_COLOR}}": BUTTON_COLOR,
        "{{BUTTON_HIGHLIGHT}}": BUTTON_HIGHLIGHT,
        "{{BUTTON_SHADOW}}": BUTTON_SHADOW,
        "{{BUTTON_CAPTION}}": BUTTON_CAPTION,
        "{{BRAND_NAME}}": BRAND_NAME,
        "{{BRAND_TAGLINE}}": BRAND_TAGLINE,
    }
