"""app — the demo surface (see app/README.md).

A package so `app.theme` is an importable module and a graph node: `repo_scan`
collapses `__init__.py` to the package id, and `resolve_uid` needs a dotted path
to key an impact set on. Deliberately empty otherwise — importing this must not
start a server or touch a store.
"""
