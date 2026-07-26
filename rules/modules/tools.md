# Module rules — `tools/`

Pulled into context only when the impact set names a module under `tools.`. This is the
mechanical half of "the model decides only *when* a rule applies": the graph routes, no model
judgment is involved in deciding these are relevant.

- Every tool **returns** a structured error and never raises. A tool that raises takes the
  loop's stopping decision away from the loop.
- Every tool docstring carries a literal "When NOT to call:" paragraph. Tool selection is decided
  by the description, not by list position.
- Constrained parameters are `Literal[...]` annotations — `schema_for` derives the JSON-Schema
  `enum` from them. Do not hand-write an `enum` into a schema.
- `validate_args` checks types and enum membership, **not numeric range**. A bound like
  `max_depth <= 64` is enforced in the tool body, and that split is deliberate.
- The graph-file I/O helpers (`_graph_path`, `_load_graph`, `_save_graph`) live in
  `tools/decisions.py` and are imported across module boundaries despite the leading underscore.
  Lifting them into a shared module is a contract change — agree it first.
