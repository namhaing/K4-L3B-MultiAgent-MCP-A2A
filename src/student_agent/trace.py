from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import Contracts


class TraceWriter:
    """Append observable workflow events. Never put prompts or chain-of-thought here."""

    def __init__(self, path: Path, contracts: Contracts) -> None:
        self.path = path
        self.contracts = contracts
        self.run_id = f"run_{secrets.token_hex(6)}"  # correlates every event of one day09 run
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        *,
        case_id: str,
        event_type: str,
        actor: str,
        target: str | None = None,
        decision_code: str | None = None,
        tool_name: str | None = None,
        evidence_refs: list[str] | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, Any]:
        event = self.build(
            case_id=case_id,
            event_type=event_type,
            actor=actor,
            target=target,
            decision_code=decision_code,
            tool_name=tool_name,
            evidence_refs=evidence_refs,
            attributes=attributes,
        )
        self.write([event])
        return event

    def build(
        self,
        *,
        case_id: str,
        event_type: str,
        actor: str,
        target: str | None = None,
        decision_code: str | None = None,
        tool_name: str | None = None,
        evidence_refs: list[str] | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": "day09-trace-event-v1",
            "event_id": f"evt_{secrets.token_urlsafe(18)}",
            "case_id": case_id,
            "event_type": event_type,
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "actor": actor,
        }
        optional = {
            "target": target,
            "decision_code": decision_code,
            "tool_name": tool_name,
            "evidence_refs": evidence_refs,
            "attributes": attributes,
        }
        event.update({key: value for key, value in optional.items() if value is not None})
        self.contracts.validate_trace(event, "trace event")
        return event

    def write(self, events: list[dict[str, Any]]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


class CaseTraceBuffer:
    """Holds one case's events and writes them only when the case completes.

    A case interrupted by a dropped MCP session is retried from scratch, so its partial events
    must never reach trace.jsonl.
    """

    def __init__(self, writer: TraceWriter) -> None:
        self.writer = writer
        self.contracts = writer.contracts
        self.run_id = writer.run_id
        self.events: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> dict[str, Any]:
        event = self.writer.build(**kwargs)
        self.events.append(event)
        return event

    def commit(self) -> None:
        self.writer.write(self.events)
        self.events = []
