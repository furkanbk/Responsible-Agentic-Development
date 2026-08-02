"""tools.utility_tools — HW4 reference tool (Phase 12, T12.5).

Owner: Berat Furkan Kocak. Not a graph tool — the point of this file is to
prove `agentlib.langchain_tools.to_langchain_tool` end-to-end on an ordinary
function before Alejandro converts the graph tools in Phase 13a. Counts as
new-tool #1 of the HW4 requirement's "at least two tools, framework-integrated,
the agent decides when to call them" (Part 2).

Read-only and reversible -> ungated (CLAUDE.md §5): a calculation can't damage
anything, so it earns no approval ceremony.
"""

from __future__ import annotations

import ast
import operator

# Whitelisted node/operator set — never `eval()`. An expression outside this
# grammar (names, calls, attribute access, comprehensions, ...) is refused as
# a structured error, not silently coerced or partially evaluated.
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_node(node: ast.AST):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"disallowed expression element: {type(node).__name__}")


def evaluate_expression(expression: str) -> dict:
    """Evaluate a numeric arithmetic expression and return its result. Read-only.

    Use when the user's request needs an actual computed number (a sum, a
    ratio, a comparison) rather than an eyeballed one — call this instead of
    doing arithmetic in your own head and stating the result as fact.
    Supports `+ - * / // % **`, unary `+ -`, parentheses, and int/float
    literals only.

    When NOT to call: the expression has variables, function calls, strings,
    or anything else outside plain arithmetic — that is not this tool's job,
    and it will refuse rather than guess.

    Returns (contract): {"expression": str, "result": <int|float>} on success,
    or {"error": "invalid_expression", "details": [<str>]} on a malformed or
    disallowed expression (own branch — never a hallucinated number).
    """
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError) as exc:
        return {"error": "invalid_expression", "details": [str(exc)]}
    return {"expression": expression, "result": result}
