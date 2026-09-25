from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Least privilege: each actor may only call the tools of its own domain.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "entity-agent": frozenset({"get_customer_history", "get_order"}),
    "order-agent": frozenset({"get_order_items", "get_product_context", "get_sellers"}),
    "payment-agent": frozenset(
        {"get_payment_timeline", "get_order_payments", "get_refund_timeline"}
    ),
    "shipment-agent": frozenset({"get_shipment_summary", "get_sellers"}),
    "policy-agent": frozenset({"get_policy"}),
}


TRANSIENT_NAME_HINTS = (
    "Timeout", "Connect", "Network", "Transport", "Protocol", "Remote", "ReadError",
    "WriteError", "Closed", "EndOfStream", "BrokenResource",
)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return any(hint in type(exc).__name__ for hint in TRANSIENT_NAME_HINTS)


@dataclass
class Evidence:
    ref: str
    tool: str
    domain: str
    actor: str
    data: Any


@dataclass
class CaseContext:
    """Per-case cache, evidence ledger and trace helper. Never shared between cases."""

    case: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    max_calls: int = 10
    call_count: int = 0
    seq: int = 0
    ledger: dict[str, Evidence] = field(default_factory=dict)
    _cache: dict[tuple[str, tuple[tuple[str, str], ...]], Evidence | None] = field(
        default_factory=dict
    )

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    def emit(self, event_type: str, actor: str, **kwargs: Any) -> None:
        """Emit with A2A correlation: run id + per-case message sequence number."""
        self.seq += 1
        attributes = {"run_id": getattr(self.trace, "run_id", None), "seq": self.seq}
        attributes.update(kwargs.pop("attributes", None) or {})
        self.trace.emit(
            case_id=self.case_id,
            event_type=event_type,
            actor=actor,
            attributes={k: v for k, v in attributes.items() if v is not None},
            **kwargs,
        )

    def refs_of(self, actor: str) -> list[str] | None:
        """Evidence refs an actor obtained, carried on its handoff (max 20 per event)."""
        refs = [ref for ref, ev in self.ledger.items() if ev.actor == actor][:20]
        return refs or None

    async def call(self, actor: str, tool: str, **arguments: str) -> Evidence | None:
        """Call a tool once per case. Returns None when the tool reports no evidence."""
        if tool not in TOOL_PERMISSIONS.get(actor, frozenset()):
            raise PermissionError(f"{actor} is not allowed to call {tool}")
        key = (tool, tuple(sorted(arguments.items())))
        if key in self._cache:
            return self._cache[key]
        if self.call_count >= self.max_calls:
            return None
        result: Evidence | None = None
        for attempt in range(2):
            self.call_count += 1
            try:
                envelope = await self.gateway.call(tool, case_id=self.case_id, **arguments)
            except (RuntimeError, ValueError):
                # Tool-level error (e.g. no refund events) or invalid envelope: no retry.
                break
            except Exception as exc:
                # Only transport/timeout failures get one bounded retry; bugs propagate.
                if not is_transient(exc) or attempt == 1:
                    raise  # after one retry, let the CLI reconnect and redo the case
                continue
            result = Evidence(
                ref=envelope["evidence_ref"],
                tool=tool,
                domain=envelope["domain"],
                actor=actor,
                data=envelope["data"],
            )
            self.ledger[result.ref] = result
            self.emit(
                "tool_result_consumed",
                actor,
                tool_name=tool,
                evidence_refs=[result.ref],
                attributes={"domain": result.domain},
            )
            break
        self._cache[key] = result
        return result

    def owns(self, ref: str) -> bool:
        return ref in self.ledger
