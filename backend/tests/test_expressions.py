from __future__ import annotations

import pytest

from app.engine.expressions import ExpressionError, evaluate, extract_expressions, referenced_nodes, render

SCOPE = {
    "trigger": {
        "body": {
            "amount": 8000,
            "name": "Ann",
            "tags": ["vip", "eu"],
            "items": [{"e": "a", "s": 90}, {"e": "b", "s": 10}],
        }
    },
    "nodes": {"classify": {"output": {"priority": "high", "confidence": 0.94}}},
    "vars": {"threshold": 5000},
}


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("trigger.body.amount > vars.threshold", True),
        ("nodes.classify.output.confidence >= 0.9 and nodes.classify.output.priority == 'high'", True),
        ("trigger.body.missing.deep", None),
        ("len(trigger.body.tags)", 2),
        ("'vip' in trigger.body.tags", True),
        ("[i.e for i in trigger.body.items if i.s > 50]", ["a"]),
        ("trigger.body.name.lower()", "ann"),
        ("default(trigger.body.nickname, 'friend')", "friend"),
        ("round(trigger.body.amount * 1.1, 2)", 8800.0),
        ("trigger.body.items[0].e", "a"),
        ("trigger.body.items[-1]['s']", 10),
        ("'high' if trigger.body.amount > 5000 else 'low'", "high"),
        ("sum(pluck(trigger.body.items, 's'))", 100),
        ("date_add('2024-01-01T00:00:00Z', days=3)", "2024-01-04T00:00:00+00:00"),
        ("regex_match('INV-2024-001', 'INV-\\\\d{4}')", True),
        ("None > 5", False),
    ],
)
def test_evaluate(expr, expected):
    assert evaluate(expr, SCOPE) == expected


@pytest.mark.parametrize(
    "expr",
    [
        "().__class__.__bases__",
        "trigger.__class__",
        "__import__('os').system('id')",
        "open('/etc/passwd')",
        "lambda: 1",
        "[x for x in range(10**9)]",
        "'a' * 10**8",
        "2 ** 10000",
        "trigger.body.name.format(x=1)",
        "eval('1')",
        "{**trigger}",
    ],
)
def test_sandbox_blocks_escapes(expr):
    with pytest.raises(ExpressionError):
        evaluate(expr, SCOPE)


def test_render_preserves_types_and_interpolates():
    assert render("{{ trigger.body.amount }}", SCOPE) == 8000
    assert render("Hi {{ trigger.body.name }}: {{ trigger.body.tags }}", SCOPE) == 'Hi Ann: ["vip", "eu"]'
    assert render({"a": ["{{ vars.threshold }}"], "b": 1}, SCOPE) == {"a": [5000], "b": 1}


def test_operation_budget():
    big = {"xs": list(range(60000))}
    with pytest.raises(ExpressionError):
        evaluate("[a for a in xs] + [b for b in xs]", big)


def test_reference_extraction():
    exprs = extract_expressions({"x": "{{ nodes.a.output.v }} and {{ nodes['b'].output }}", "y": ["{{ trigger }}"]})
    assert len(exprs) == 3
    assert referenced_nodes(exprs[0]) == {"a"}
    assert referenced_nodes(exprs[1]) == {"b"}
