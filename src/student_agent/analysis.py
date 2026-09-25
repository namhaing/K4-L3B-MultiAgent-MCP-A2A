"""Deterministic evidence analysis for L3B cases (pure functions, no MCP calls).

The gateway returns several record versions for the same order (conflicting sources). The version
that the complaint is about is the most recent one purchased on or before the case `opened_at`.
Lifecycle events (payment, refund, shipment) are attributed to the version whose purchase time is
the latest one not after the event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Any

REFUND_DONE = {"completed", "succeeded", "success", "processed", "refunded", "confirmed"}
LATE_TOPICS = {"late_delivery_seller", "late_delivery_logistics"}
REFUND_TOPICS = {"refund_pending", "refund_failed"}
NO_CLAIM_ISSUES = {"unsupported_claim", "valid_split_payment"}


def parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def unique(values: list[Any]) -> list[Any]:
    seen: list[Any] = []
    for value in values:
        if value is not None and value not in seen:
            seen.append(value)
    return seen


def _observable(row: dict[str, Any], opened_at: datetime | None) -> bool:
    """The complaint can only concern an order purchased, and due, by the time it was opened."""
    if opened_at is None:
        return True
    purchased = parse_dt(row.get("order_purchase_timestamp"))
    estimated = parse_dt(row.get("order_estimated_delivery_date"))
    due = estimated is None or estimated <= opened_at
    return bool(purchased and purchased <= opened_at and due)


def select_version(rows: list[dict[str, Any]], opened_at: datetime | None) -> dict[str, Any] | None:
    """Latest version observable at opened_at; else latest purchased before it; else earliest."""
    dated = [(ts, row) for row in rows if (ts := parse_dt(row.get("order_purchase_timestamp")))]
    if not dated:
        return rows[0] if rows else None
    for pool in (
        [(ts, row) for ts, row in dated if _observable(row, opened_at)],
        [(ts, row) for ts, row in dated if opened_at is None or ts <= opened_at],
    ):
        if pool:
            return max(pool, key=lambda pair: pair[0])[1]
    return min(dated, key=lambda pair: pair[0])[1]


def owner_start(ts: datetime | None, starts: list[datetime]) -> datetime | None:
    """Purchase time of the version that owns an event happening at `ts`."""
    if ts is None or not starts:
        return None
    before = [start for start in starts if start <= ts]
    return max(before) if before else min(starts)


@dataclass
class Findings:
    order_id: str
    version: dict[str, Any]
    other_versions: list[dict[str, Any]]
    items: list[dict[str, Any]] = field(default_factory=list)
    captures: list[float] = field(default_factory=list)
    mismatch_open: bool = False
    refund_events: list[dict[str, Any]] = field(default_factory=list)
    shipment_events: list[dict[str, Any]] = field(default_factory=list)
    has_payment_evidence: bool = False
    has_refund_evidence: bool = False
    has_shipment_evidence: bool = False

    @property
    def status(self) -> str:
        return str(self.version.get("order_status") or "")

    @property
    def order_value(self) -> float:
        total = sum(money(i.get("price")) + money(i.get("freight_value")) for i in self.items)
        return round(total, 2)

    @property
    def captured_total(self) -> float:
        return round(sum(self.captures), 2)

    @property
    def refunded_total(self) -> float:
        return round(
            sum(
                money(e.get("amount_brl"))
                for e in self.refund_events
                if str(e.get("status", "")).lower() in REFUND_DONE
            ),
            2,
        )

    @property
    def refund_state(self) -> str | None:
        if not self.refund_events:
            return None
        latest = max(self.refund_events, key=lambda e: parse_dt(e.get("event_at")) or datetime.min)
        status = str(latest.get("status", "")).lower()
        if status in REFUND_DONE:
            return "refunded"
        if status in {"failed", "rejected", "declined", "error"}:
            return "failed"
        return "pending"

    @property
    def seller_ids(self) -> list[str]:
        return unique([i.get("seller_id") for i in self.items])

    @property
    def item_ids(self) -> list[str]:
        return unique([i.get("order_item_id") for i in self.items])

    @property
    def delivered_at(self) -> datetime | None:
        return parse_dt(self.version.get("order_delivered_customer_date"))

    @property
    def estimated_at(self) -> datetime | None:
        return parse_dt(self.version.get("order_estimated_delivery_date"))

    @property
    def carrier_at(self) -> datetime | None:
        return parse_dt(self.version.get("order_delivered_carrier_date"))

    @property
    def late_by_dates(self) -> bool:
        delivered, estimated = self.delivered_at, self.estimated_at
        return bool(delivered and estimated and delivered.date() > estimated.date())

    @property
    def late_event(self) -> dict[str, Any] | None:
        for event in self.shipment_events:
            if event.get("event_type") == "delivered_late" and event.get("status") != "rejected":
                return event
        return None

    @property
    def is_late(self) -> bool:
        return self.late_by_dates or self.late_event is not None

    @property
    def late_seller_ids(self) -> list[str]:
        carrier = self.carrier_at
        if carrier is None:
            return []
        return unique(
            [
                i.get("seller_id")
                for i in self.items
                if (limit := parse_dt(i.get("shipping_limit_date"))) and carrier > limit
            ]
        )

    @property
    def late_party(self) -> str | None:
        if not self.is_late:
            return None
        event = self.late_event
        if event and event.get("actor") in {"seller", "logistics_provider"}:
            return str(event["actor"])
        return "seller" if self.late_seller_ids else "logistics_provider"

    @property
    def split_subset(self) -> tuple[float, ...] | None:
        """Two or more captures that together pay exactly the order value."""
        value = self.order_value
        if value <= 0 or len(self.captures) > 12:
            return None
        for size in range(len(self.captures), 1, -1):
            for combo in combinations(self.captures, size):
                if abs(sum(combo) - value) <= 0.01:
                    return combo
        return None

    @property
    def duplicate_capture(self) -> bool:
        """The same amount captured repeatedly, not explained by a split of the order value."""
        for amount in set(self.captures):
            count = self.captures.count(amount)
            if count >= 2 and abs(amount * count - self.order_value) > 0.01:
                return True
        return False

    @property
    def split_payment(self) -> bool:
        return self.split_subset is not None

    @property
    def timeline_complete(self) -> bool:
        keys = (
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        )
        return all(self.version.get(key) for key in keys)


def build_findings(
    order_id: str,
    opened_at: datetime | None,
    history_rows: list[dict[str, Any]],
    order_row: dict[str, Any] | None,
) -> Findings | None:
    rows = [row for row in history_rows if row.get("order_id") == order_id]
    if not rows and order_row:
        rows = [order_row]
    version = select_version(rows, opened_at)
    if version is None:
        return None
    others = [row for row in rows if row is not version]
    return Findings(order_id=order_id, version=version, other_versions=others)


def _starts(findings: Findings) -> list[datetime]:
    rows = [findings.version, *findings.other_versions]
    return [ts for row in rows if (ts := parse_dt(row.get("order_purchase_timestamp")))]


def _mine(findings: Findings, ts: datetime | None) -> bool:
    starts = _starts(findings)
    if len(starts) <= 1:
        return True
    return owner_start(ts, starts) == parse_dt(findings.version.get("order_purchase_timestamp"))


def attach_items(findings: Findings, items: list[dict[str, Any]]) -> None:
    mine = [i for i in items if _mine(findings, parse_dt(i.get("shipping_limit_date")))]
    keys = ("order_item_id", "seller_id", "shipping_limit_date", "price", "freight_value")
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in mine or items[:1]:
        deduped.setdefault(tuple(item.get(k) for k in keys), item)  # tied versions repeat rows
    findings.items = list(deduped.values())


def _distinct(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tied record versions repeat identical lifecycle events; count each event once."""
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        key = tuple(event.get(k) for k in ("event_at", "event_type", "amount_brl", "status"))
        seen.setdefault(key, event)
    return list(seen.values())


def attach_payments(findings: Findings, timeline: dict[str, Any]) -> None:
    findings.has_payment_evidence = True
    for event in _distinct(timeline.get("events") or []):
        if not _mine(findings, parse_dt(event.get("event_at"))):
            continue
        kind = event.get("event_type")
        status = str(event.get("status", "")).lower()
        if kind == "captured" and status in {"confirmed", "captured", "succeeded", "completed"}:
            findings.captures.append(money(event.get("amount_brl")))
        elif kind == "reconciliation_mismatch" and status not in {"resolved", "closed"}:
            findings.mismatch_open = True


def attach_refunds(findings: Findings, timeline: dict[str, Any]) -> None:
    findings.has_refund_evidence = True
    findings.refund_events = [
        e for e in _distinct(timeline.get("events") or [])
        if _mine(findings, parse_dt(e.get("event_at")))
    ]


def attach_shipment(findings: Findings, summary: dict[str, Any]) -> None:
    """A delivery event belongs to the version delivered on that day; otherwise by timing."""
    findings.has_shipment_evidence = True
    rows = [findings.version, *findings.other_versions]
    delivered = {
        (row.get("order_purchase_timestamp"), d.date())
        for row in rows
        if (d := parse_dt(row.get("order_delivered_customer_date")))
    }
    own = findings.version.get("order_purchase_timestamp")
    events = []
    for event in _distinct(summary.get("events") or []):
        at = parse_dt(event.get("event_at"))
        owners = {start for start, day in delivered if at and day == at.date()}
        if (own in owners) if owners else _mine(findings, at):
            events.append(event)
    findings.shipment_events = events


def detect_issues(f: Findings) -> list[str]:
    """Every issue the selected version supports, most specific first."""
    issues: list[str] = []
    refund_state = f.refund_state
    unrefunded = f.captured_total > f.refunded_total + 0.01
    if refund_state == "failed":
        issues.append("refund_failed")
    if refund_state == "pending":
        issues.append("refund_pending")
    if f.status == "canceled" and unrefunded:
        issues.append("canceled_order_paid")
    if f.status == "unavailable" and unrefunded:
        issues.append("unavailable_order_paid")
    if f.mismatch_open:
        issues.append("payment_mismatch")
    if f.duplicate_capture:
        issues.append("duplicate_charge")
    if f.late_party == "seller":
        issues.append("late_delivery_seller")
    if f.late_party == "logistics_provider":
        issues.append("late_delivery_logistics")
    if f.split_payment:
        issues.append("valid_split_payment")
    return issues or ["unsupported_claim"]


def detect_issue(f: Findings, claimed: str | None = None) -> str:
    """Primary issue; when the evidence supports several, the claimed one wins the tie."""
    issues = detect_issues(f)
    return claimed if claimed in issues else issues[0]


def shipment_verdict(f: Findings, issue: str) -> str:
    if issue == "late_delivery_seller":
        return "seller_delay"
    if issue == "late_delivery_logistics":
        return "logistics_delay"
    if f.delivered_at is None:
        return "insufficient_evidence"
    return "logistics_delay" if f.is_late else "on_time"


def payment_verdict(f: Findings, issue: str) -> str:
    if not f.has_payment_evidence:
        return "insufficient_evidence"
    mapping = {
        "refund_failed": "refund_failed",
        "refund_pending": "refund_pending",
        "payment_mismatch": "capture_mismatch",
        "duplicate_charge": "duplicate_capture",
    }
    if issue in mapping:
        return mapping[issue]
    if f.refund_state == "refunded" and f.refunded_total >= f.captured_total - 0.01:
        return "refunded"
    return "reconciled"
