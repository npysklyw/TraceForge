"""Pure, versioned scorer registry. No provider or database access."""

import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import cast

from pydantic import JsonValue

from traceforge.scoring.expectations import (
    ExactOutput,
    Expectation,
    MaxCost,
    MaxLatency,
    NormalizedOutput,
    Pricing,
    RequiredSubstring,
    ToolArguments,
    ToolSelection,
)


@dataclass(frozen=True)
class ToolEvidence:
    name: str
    arguments: dict[str, JsonValue]
    validated: bool = True


@dataclass(frozen=True)
class Evidence:
    output: JsonValue = None
    tools: list[ToolEvidence] = field(default_factory=list)
    latency_ms: float | None = None
    cost_usd: Decimal | None = None
    execution_succeeded: bool = True


@dataclass(frozen=True)
class Verdict:
    passed: bool
    explanation: str
    expected: JsonValue
    observed: JsonValue
    category: str


def json_equal(expected: JsonValue, observed: JsonValue, *, partial: bool = False) -> bool:
    # JSON booleans are not numbers; Python's True == 1 must not leak into scoring.
    if isinstance(expected, bool) or isinstance(observed, bool):
        return type(expected) is type(observed) and expected == observed
    if isinstance(expected, dict) and isinstance(observed, dict):
        return (partial or expected.keys() == observed.keys()) and all(
            key in observed and json_equal(value, observed[key], partial=partial)
            for key, value in expected.items()
        )
    if isinstance(expected, list) and isinstance(observed, list):
        return len(expected) == len(observed) and all(
            json_equal(a, b) for a, b in zip(expected, observed, strict=True)
        )
    return expected == observed


def verdict(ok: bool, expected: JsonValue, observed: JsonValue, category: str) -> Verdict:
    return Verdict(
        ok,
        "Expectation satisfied." if ok else "Expectation not satisfied.",
        expected,
        observed,
        category,
    )


def exact(expectation: Expectation, evidence: Evidence) -> Verdict:
    item = cast(ExactOutput, expectation)
    return verdict(
        json_equal(item.expected, evidence.output),
        item.expected,
        evidence.output,
        "output_correctness",
    )


def normalize(value: str, mode: str) -> str:
    if mode == "case_insensitive":
        return value.casefold()
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalized(expectation: Expectation, evidence: Evidence) -> Verdict:
    item = cast(NormalizedOutput, expectation)
    ok = isinstance(evidence.output, str) and normalize(item.expected, item.mode) == normalize(
        evidence.output, item.mode
    )
    return verdict(ok, item.expected, evidence.output, "output_correctness")


def substring(expectation: Expectation, evidence: Evidence) -> Verdict:
    item = cast(RequiredSubstring, expectation)
    expected, observed = item.expected, evidence.output
    ok = isinstance(observed, str) and (
        expected in observed if item.case_sensitive else expected.casefold() in observed.casefold()
    )
    return verdict(ok, expected, observed, "output_correctness")


def selection(expectation: Expectation, evidence: Evidence) -> Verdict:
    item = cast(ToolSelection, expectation)
    observed = [tool.name for tool in evidence.tools]
    ok = (
        observed == item.expected
        if item.mode == "exact"
        else (Counter(item.expected) <= Counter(observed))
    )
    return verdict(ok, list(item.expected), list(observed), "tool_selection")


def arguments(expectation: Expectation, evidence: Evidence) -> Verdict:
    item = cast(ToolArguments, expectation)
    tools = [tool for tool in evidence.tools if tool.name == item.tool_name]
    tool = tools[item.occurrence] if item.occurrence < len(tools) else None
    observed = tool.arguments if tool and tool.validated else None
    ok = observed is not None and json_equal(
        item.expected, observed, partial=item.mode == "partial"
    )
    result = verdict(ok, item.expected, observed, "tool_arguments")
    if observed is None:
        return Verdict(
            False,
            "Requested tool occurrence is missing or arguments were not validated.",
            item.expected,
            None,
            "tool_arguments",
        )
    return result


def latency(expectation: Expectation, evidence: Evidence) -> Verdict:
    item = cast(MaxLatency, expectation)
    return verdict(
        evidence.latency_ms is not None and evidence.latency_ms <= item.maximum_ms,
        item.maximum_ms,
        evidence.latency_ms,
        "latency",
    )


def cost(expectation: Expectation, evidence: Evidence) -> Verdict:
    item = cast(MaxCost, expectation)
    if evidence.cost_usd is None:
        return Verdict(
            False,
            "Cost unavailable: configure pricing and supply complete token usage.",
            str(item.maximum_usd),
            None,
            "cost",
        )
    return verdict(
        evidence.cost_usd <= item.maximum_usd, str(item.maximum_usd), str(evidence.cost_usd), "cost"
    )


SCORERS: dict[str, Callable[[Expectation, Evidence], Verdict]] = {
    "exact_output": exact,
    "normalized_output": normalized,
    "required_substring": substring,
    "tool_selection": selection,
    "tool_arguments": arguments,
    "max_latency": latency,
    "max_cost": cost,
}


def score(expectation: Expectation, evidence: Evidence) -> Verdict:
    if not evidence.execution_succeeded:
        raise ValueError("Execution errors are not scoreable")
    return SCORERS[expectation.type](expectation, evidence)


def estimate_cost(
    pricing: Pricing | None, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    with localcontext() as context:
        context.prec = 50
        return (
            pricing.input_usd_per_million * input_tokens
            + pricing.output_usd_per_million * output_tokens
        ) / Decimal(1_000_000)
