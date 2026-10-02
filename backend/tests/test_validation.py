from __future__ import annotations

from app.engine.validation import validate_definition
from tests.factories import definition, edge, linear, node


def messages(defn):
    report = validate_definition(defn)
    return report, [i.message for i in report.errors]


def test_valid_linear_workflow():
    report, errors = messages(linear(("set", "logic.set_variable", {"assignments": {"x": "{{ trigger.a }}"}})))
    assert report.valid, errors


def test_requires_single_trigger():
    _, errors = messages(definition([node("a", "logic.set_variable", {"assignments": {}})], []))
    assert any("exactly one trigger" in e for e in errors)
    _, errors = messages(definition([node("a", "trigger.manual"), node("b", "trigger.manual")], []))
    assert any("Only one trigger" in e for e in errors)


def test_unknown_type_and_bad_config():
    _, errors = messages(linear(("x", "nope.node", {})))
    assert any("Unknown node type" in e for e in errors)
    _, errors = messages(linear(("d", "logic.delay", {"amount": -5})))
    assert errors


def test_expressions_skip_static_type_validation():
    report, errors = messages(linear(("d", "logic.delay", {"amount": "{{ trigger.minutes }}", "unit": "minutes"})))
    assert report.valid, errors


def test_cycle_detection():
    d = definition(
        [
            node("t", "trigger.manual"),
            node("a", "logic.set_variable", {"assignments": {}}),
            node("b", "logic.set_variable", {"assignments": {}}),
        ],
        [edge("t", "a"), edge("a", "b"), edge("b", "a")],
    )
    _, errors = messages(d)
    assert any("Cycle" in e for e in errors)


def test_invalid_handles():
    d = definition(
        [
            node("t", "trigger.manual"),
            node("if", "logic.if", {"condition": "{{ true }}"}),
            node("a", "logic.set_variable", {"assignments": {}}),
        ],
        [edge("t", "if"), edge("if", "a", "maybe")],
    )
    _, errors = messages(d)
    assert any("no output handle 'maybe'" in e for e in errors)


def test_switch_dynamic_handles():
    d = definition(
        [
            node("t", "trigger.manual"),
            node(
                "sw", "logic.switch", {"value": "{{ trigger.kind }}", "cases": [{"handle": "sales", "value": "sales"}]}
            ),
            node("a", "logic.set_variable", {"assignments": {}}),
            node("b", "logic.set_variable", {"assignments": {}}),
        ],
        [edge("t", "sw"), edge("sw", "a", "sales"), edge("sw", "b", "default")],
    )
    report, errors = messages(d)
    assert report.valid, errors


def test_loop_body_isolation():
    d = definition(
        [
            node("t", "trigger.manual"),
            node("loop", "logic.loop", {"items": "{{ trigger.items }}"}),
            node("body", "logic.set_variable", {"assignments": {}}),
            node("outside", "logic.set_variable", {"assignments": {}}),
        ],
        [edge("t", "loop"), edge("loop", "body", "body"), edge("t", "outside"), edge("outside", "body")],
    )
    _, errors = messages(d)
    assert any("inside loop" in e for e in errors)


def test_expression_reference_checks():
    d = linear(("a", "logic.set_variable", {"assignments": {"x": "{{ nodes.ghost.output }}"}}))
    _, errors = messages(d)
    assert any("unknown node 'ghost'" in e for e in errors)
    d = linear(("a", "logic.set_variable", {"assignments": {"x": "{{ nodes.trigger_typo( }}"}}))
    _, errors = messages(d)
    assert any("Expression error" in e for e in errors)


def test_schedule_semantic_validation():
    _, errors = messages(linear(trigger="trigger.schedule", trigger_config={"cron": "not a cron"}))
    assert any("Invalid cron" in e for e in errors)


def test_approval_requires_approvers():
    _, errors = messages(linear(("ap", "human.approval", {"title": "Approve?"})))
    assert any("approver" in e for e in errors)


def test_stored_definitions_with_retired_null_setting_still_load():
    import pytest
    from pydantic import ValidationError

    from app.engine.definition import WorkflowDefinition

    base = {"nodes": [{"id": "t", "type": "trigger.manual"}], "edges": []}
    d = WorkflowDefinition.model_validate({**base, "settings": {"concurrency_key": None}})
    assert "concurrency_key" not in d.settings.model_dump()
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate({**base, "settings": {"concurrency_key": "{{ trigger.id }}"}})
