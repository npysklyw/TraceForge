"""Fictional support data. No clocks, I/O, credentials, or external side effects."""

from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, JsonValue, StringConstraints

from traceforge.tools.registry import Tool, ToolRegistry


class OrderArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: Annotated[str, StringConstraints(pattern=r"^ORDER-[A-Z0-9-]+$", max_length=60)]


ORDERS = MappingProxyType(
    {
        "ORDER-1001": ("delivered", 12, 4200),
        "ORDER-1002": ("delivered", 60, 1800),
        "ORDER-1003": ("processing", 0, 9900),
    }
)
REFUND_WINDOW_DAYS = 30


def get_order(args: OrderArguments) -> dict[str, JsonValue]:
    status, age, amount = ORDERS[args.order_id]
    return {
        "order_id": args.order_id,
        "status": status,
        "days_since_delivery": age,
        "amount_cents": amount,
        "currency": "USD",
    }


def check_refund_eligibility(args: OrderArguments) -> dict[str, JsonValue]:
    status, age, _ = ORDERS[args.order_id]
    eligible = status == "delivered" and age <= REFUND_WINDOW_DAYS
    return {
        "order_id": args.order_id,
        "eligible": eligible,
        "policy": "Fictional delivered orders within 30 days are eligible.",
    }


def create_refund_request(args: OrderArguments) -> dict[str, JsonValue]:
    eligible = check_refund_eligibility(args)["eligible"]
    if not eligible:
        return {"order_id": args.order_id, "status": "ineligible"}
    return {
        "order_id": args.order_id,
        "request_id": f"REFUND-{args.order_id}",
        "status": "requested",
        "synthetic": True,
    }


def support_registry() -> ToolRegistry:
    return ToolRegistry(
        (
            Tool("get_order", "Look up a fictional order.", OrderArguments, get_order),
            Tool(
                "check_refund_eligibility",
                "Check the fictional refund policy.",
                OrderArguments,
                check_refund_eligibility,
            ),
            Tool(
                "create_refund_request",
                "Return a deterministic fictional refund request.",
                OrderArguments,
                create_refund_request,
            ),
        )
    )
