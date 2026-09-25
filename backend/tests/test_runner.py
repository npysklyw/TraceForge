import json
from dataclasses import asdict

import pytest

from benchwarden.application.redaction import TraceSanitizer
from benchwarden.application.runner import AgentRunner
from benchwarden.providers.base import ModelRequest, ModelResponse, ToolRequest, Usage
from benchwarden.providers.fake import FINAL, SCENARIOS, FakeModelProvider
from benchwarden.tools.registry import InvalidToolArguments, Tool, ToolRegistry
from benchwarden.tools.support import OrderArguments, support_registry


def execute(scenario, *, provider=None, registry=None, max_steps=8, inputs=None):
    inputs = inputs or {"scenario": scenario}
    events, tools = [], []
    outcome = AgentRunner(provider or FakeModelProvider(), registry or support_registry()).run(
        ModelRequest(
            model="deterministic-v1", system_prompt="Fictional support", parameters={}, input=inputs
        ),
        max_steps=max_steps,
        sanitizer=TraceSanitizer(inputs),
        on_event=events.append,
        on_tool=tools.append,
    )
    return outcome, events, tools


@pytest.mark.parametrize(
    "scenario,error",
    [
        ("text_only", None),
        ("valid_tool", None),
        ("refund_flow", None),
        ("unknown_tool", "unknown_tool"),
        ("invalid_arguments", "invalid_tool_arguments"),
        ("timeout", "provider_timeout"),
        ("provider_error", "provider_error"),
        ("tool_failure", "tool_error"),
    ],
)
def test_fake_scenarios(scenario, error):
    outcome, events, tools = execute(scenario)
    assert outcome.error_type == error
    assert outcome.output == (None if error else FINAL.text)
    assert events[0].kind == "case_started"
    assert events[-1].kind == "case_completed"
    if error:
        assert events[-2].payload["type"] == error
        assert outcome.error
    assert outcome.finished_at >= outcome.started_at
    assert outcome.latency_ms >= 0
    for event in events:
        assert event.finished_at >= event.started_at
        assert event.latency_ms >= 0
    if scenario == "invalid_arguments":
        assert not tools[0].arguments_validated
        assert tools[0].arguments == {}
    if scenario == "tool_failure":
        assert tools[0].arguments_validated


def test_tool_trace_order_and_usage():
    outcome, events, tools = execute("valid_tool")
    assert [event.kind for event in events] == [
        "case_started",
        "model_request",
        "model_response",
        "tool_request",
        "tool_result",
        "model_request",
        "model_response",
        "case_completed",
    ]
    assert outcome.usage == Usage(input_tokens=22, output_tokens=11, total_tokens=33)
    assert tools[0].name == "get_order"
    assert tools[0].arguments == {"order_id": "ORDER-1001"}
    assert tools[0].output["amount_cents"] == 4200
    assert events[5].payload["messages"][1]["call_id"] == "fake-call-1"


def test_all_support_tools_and_repeatability():
    first, events, tools = execute("refund_flow")
    second, _, second_tools = execute("refund_flow")
    assert first.output == second.output
    assert first.usage == Usage(input_tokens=42, output_tokens=21, total_tokens=63)
    assert [tool.name for tool in tools] == [
        "get_order",
        "check_refund_eligibility",
        "create_refund_request",
    ]
    assert [tool.output for tool in tools] == [tool.output for tool in second_tools]
    assert tools[-1].output["request_id"] == "REFUND-ORDER-1001"


def test_tool_implementation_failure_never_exposes_exception():
    def broken(_args):
        raise RuntimeError("password=PRIVATE-EXCEPTION /home/private/file")

    registry = ToolRegistry((Tool("get_order", "Broken synthetic tool", OrderArguments, broken),))
    outcome, events, tools = execute("valid_tool", registry=registry)
    assert outcome.error_type == "tool_error"
    assert "PRIVATE" not in repr((outcome, events, tools))
    assert tools[0].error == "The tool could not complete the request."


def test_step_limit_terminates_loop():
    class Loop:
        def complete(self, request):
            return ModelResponse(
                tool_call=ToolRequest(
                    call_id="loop", name="get_order", arguments={"order_id": "ORDER-1001"}
                )
            )

    outcome, events, tools = execute("text_only", provider=Loop(), max_steps=2)
    assert outcome.error_type == "step_limit"
    assert len(tools) == 2
    assert outcome.usage.total_tokens is None


def test_internal_fields_and_known_secrets_excluded():
    class LeakyProvider:
        def complete(self, request):
            return ModelResponse.model_validate(
                {
                    "text": "reply SECRET-VALUE sk-example-token <think>HIDDEN</think>",
                    "reasoning": "PRIVATE-REASONING",
                    "provider_internal": {"api_key": "PRIVATE-KEY"},
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 2,
                        "total_tokens": 5,
                        "reasoning": "PRIVATE-USAGE",
                    },
                }
            )

    outcome, events, tools = execute(
        "text_only",
        provider=LeakyProvider(),
        inputs={
            "scenario": "text_only",
            "api_key": "SECRET-VALUE",
            "nested": {"internal_reasoning": "PRIVATE-INPUT"},
        },
    )
    serialized = json.dumps([asdict(outcome), *[asdict(event) for event in events]], default=str)
    for forbidden in (
        "SECRET-VALUE",
        "sk-example-token",
        "HIDDEN",
        "PRIVATE",
        "api_key",
        "reasoning",
    ):
        assert forbidden not in serialized
    assert "[REDACTED]" in outcome.output


def test_provider_exception_sanitized_and_unknown_usage_stays_null():
    class Broken:
        def complete(self, request):
            raise RuntimeError("Bearer SECRET plus internal reasoning")

    outcome, events, _ = execute("text_only", provider=Broken())
    assert outcome.error_type == "provider_error"
    assert outcome.usage.total_tokens is None
    assert "SECRET" not in repr(events)


def test_registry_rejects_duplicate_names_and_typed_extra_arguments():
    registry = support_registry()
    with pytest.raises(InvalidToolArguments):
        registry.prepare(
            ToolRequest(
                call_id="1",
                name="get_order",
                arguments={
                    "order_id": "ORDER-1001",
                    "execute": "arbitrary code",
                },
            )
        )
    tool = Tool("same", "demo", OrderArguments, lambda args: None)
    with pytest.raises(ValueError, match="unique"):
        ToolRegistry((tool, tool))


def test_scenario_catalog_cannot_be_mutated_by_response_consumers():
    request = ModelRequest(
        model="fake", system_prompt="", parameters={}, input={"scenario": "valid_tool"}
    )
    reply = FakeModelProvider().complete(request)
    reply.tool_call.arguments["order_id"] = "MUTATED"
    assert FakeModelProvider().complete(request).tool_call.arguments["order_id"] == "ORDER-1001"
    assert set(SCENARIOS) >= {"text_only", "timeout", "provider_error"}


def test_redaction_removes_known_credential_values_echoed_without_labels():
    sanitizer = TraceSanitizer(
        "api_key=KNOWN-CREDENTIAL", {"authorization": "Bearer OTHER-CREDENTIAL"}
    )
    assert "KNOWN-CREDENTIAL" not in sanitizer.text("Echo KNOWN-CREDENTIAL")
    assert "OTHER-CREDENTIAL" not in sanitizer.text("Echo OTHER-CREDENTIAL")


def test_fake_provider_and_tools_need_no_network_or_files(monkeypatch):
    import builtins
    import socket

    def prohibited(*args, **kwargs):
        raise AssertionError("Unexpected external I/O")

    monkeypatch.setattr(socket, "socket", prohibited)
    monkeypatch.setattr(builtins, "open", prohibited)
    outcome, _, tools = execute("refund_flow")
    assert outcome.error_type is None
    assert len(tools) == 3
