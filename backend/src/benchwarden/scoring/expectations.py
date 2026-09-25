from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

VERSION = "1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NamedExpectation(StrictModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.-]+$")


class ExactOutput(NamedExpectation):
    type: Literal["exact_output"]
    expected: JsonValue


class NormalizedOutput(NamedExpectation):
    type: Literal["normalized_output"]
    expected: str
    mode: Literal["case_insensitive", "normalized"] = "normalized"


class RequiredSubstring(NamedExpectation):
    type: Literal["required_substring"]
    expected: str = Field(min_length=1)
    case_sensitive: bool = True


class ToolSelection(NamedExpectation):
    type: Literal["tool_selection"]
    expected: list[Annotated[str, Field(min_length=1, max_length=200)]]
    mode: Literal["exact", "required"] = "exact"


class ToolArguments(NamedExpectation):
    type: Literal["tool_arguments"]
    tool_name: str = Field(min_length=1, max_length=200)
    occurrence: int = Field(default=0, ge=0, strict=True)
    expected: dict[str, JsonValue]
    mode: Literal["equality", "partial"] = "equality"


class MaxLatency(NamedExpectation):
    type: Literal["max_latency"]
    maximum_ms: float = Field(ge=0)


Money = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=12)]


class MaxCost(NamedExpectation):
    type: Literal["max_cost"]
    maximum_usd: Money


Expectation = Annotated[
    ExactOutput
    | NormalizedOutput
    | RequiredSubstring
    | ToolSelection
    | ToolArguments
    | MaxLatency
    | MaxCost,
    Field(discriminator="type"),
]


class Expectations(StrictModel):
    items: list[Expectation] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        if len({item.name for item in self.items}) != len(self.items):
            raise ValueError("Expectation names must be unique within a test case")
        return self


def parse_expectations(value: object) -> list[Expectation]:
    return Expectations(items=TypeAdapter(list[Expectation]).validate_python(value)).items


class Pricing(StrictModel):
    currency: Literal["USD"] = "USD"
    input_usd_per_million: Money
    output_usd_per_million: Money
