"""agentlib — the raw-Python agent runtime for RADF (HW1).

Public surface (owned by Berat, Phase 0):
- core:    call() -> Result, CHEAP/STRONG/MODELS, estimate_cost(), show()
- schemas: schema_for(fn)
- guards:  validate_args(), check_output(), detect_stall(), GATED, requires_approval()
- loop:    run_agent()

No agent/LLM framework is used or allowed in HW1 (see CLAUDE.md §4). This is the
Session 3 notebook scaffold, reorganized into a package.
"""
