from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .analysis import (
    LATE_TOPICS,
    NO_CLAIM_ISSUES,
    REFUND_TOPICS,
    Findings,
    attach_items,
    attach_payments,
    attach_refunds,
    attach_shipment,
    build_findings,
    detect_issue,
    detect_issues,
    money,
    parse_dt,
    payment_verdict,
    shipment_verdict,
    unique,
)
from .case_context import CaseContext, is_transient
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ORDER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CONFLICT_FIELDS = ("order_status", "order_purchase_timestamp")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: entity → order → payment → shipment → policy → conflict → verifier."""
    ctx = CaseContext(case, gateway, trace)
    try:
        output = await _investigate(ctx)
        trace.contracts.validate_output(output, f"outputs/{ctx.case_id}.json")
        _debug_dump(ctx)
        return output
    except Exception as exc:  # never abort the whole run because of one case
        if is_transient(exc):
            raise  # dropped MCP session: the CLI reconnects and redoes this case
        output = _fallback(ctx, type(exc).__name__)
        trace.contracts.validate_output(output, f"outputs/{ctx.case_id}.json")
        return output


async def _investigate(ctx: CaseContext) -> dict[str, Any]:
    case = ctx.case
    request = case.get("customer_request") or {}
    claims = request.get("claims") or []
    topic = next(
        (c.get("topic") for c in claims if c.get("topic") != "requested_full_refund"), None
    )
    opened_at = parse_dt(case.get("opened_at"))

    # --- Entity/customer agent -------------------------------------------------------------
    ctx.emit("task_assigned", "coordinator", target="entity-agent", decision_code="RESOLVE_ENTITY")
    history_rows: list[dict[str, Any]] = []
    customer_unique_id = None
    hint = case.get("customer_unique_id_hint")
    if hint:
        history = await ctx.call("entity-agent", "get_customer_history", customer_unique_id=hint)
        if history and isinstance(history.data, dict):
            history_rows = list(history.data.get("orders") or [])
            customer_unique_id = history.data.get("customer_unique_id") or hint

    candidates = list(case.get("candidate_order_ids") or [])
    claimed = request.get("claimed_order_id")
    ordered = unique([claimed, *candidates])
    history_ids = {row.get("order_id") for row in history_rows}
    resolved_id = None
    order_row = None
    for candidate in ordered:
        if not isinstance(candidate, str) or not ORDER_ID_PATTERN.fullmatch(candidate):
            continue  # synthetic placeholders such as "candidate-001" are not real orders
        if history_rows and candidate not in history_ids and candidate != claimed:
            continue
        order = await ctx.call("entity-agent", "get_order", order_id=candidate)
        if order and isinstance(order.data, dict):
            resolved_id, order_row = candidate, order.data
            break
    rejected = [c for c in candidates if c != resolved_id]
    status = "resolved" if resolved_id else "not_found"
    ctx.emit(
        "handoff",
        "entity-agent",
        target="coordinator",
        decision_code=status.upper(),
        evidence_refs=ctx.refs_of("entity-agent"),
        attributes={"rejected_candidates": len(rejected)},
    )
    if not resolved_id:
        return _fallback(ctx, "ENTITY_NOT_FOUND", rejected=rejected, customer=customer_unique_id)

    findings = build_findings(resolved_id, opened_at, history_rows, order_row)
    if findings is None:
        return _fallback(ctx, "NO_ORDER_VERSION", rejected=rejected, customer=customer_unique_id)

    # --- Order/product agent ---------------------------------------------------------------
    ctx.emit("task_assigned", "coordinator", target="order-agent", decision_code="LOAD_ITEMS")
    items = await ctx.call("order-agent", "get_order_items", order_id=resolved_id)
    if items and isinstance(items.data, list):
        attach_items(findings, items.data)
    ctx.emit("handoff", "order-agent", target="coordinator", decision_code="ITEMS_LOADED",
             evidence_refs=ctx.refs_of("order-agent"), attributes={"items": len(findings.items)})

    # --- Payment/refund agent --------------------------------------------------------------
    ctx.emit("task_assigned", "coordinator", target="payment-agent", decision_code="RECONCILE")
    payments = await ctx.call("payment-agent", "get_payment_timeline", order_id=resolved_id)
    if payments and isinstance(payments.data, dict):
        attach_payments(findings, payments.data)
    # Refund lifecycle is only fetched for refund complaints: for other orders the gateway has
    # no refund events and the call returns an error, costing budget without evidence.
    if topic in REFUND_TOPICS:
        refunds = await ctx.call("payment-agent", "get_refund_timeline", order_id=resolved_id)
        if refunds and isinstance(refunds.data, dict):
            attach_refunds(findings, refunds.data)
    ctx.emit("handoff", "payment-agent", target="coordinator",
             decision_code=payment_verdict(findings, detect_issue(findings)).upper(),
             evidence_refs=ctx.refs_of("payment-agent"),
             attributes={"captured_total_brl": findings.captured_total})

    # --- Shipment agent --------------------------------------------------------------------
    # Refuting a claim needs the delivery timeline too, not only the payment side.
    refuting = detect_issue(findings) == "unsupported_claim"
    if findings.late_by_dates or topic in LATE_TOPICS or refuting:
        ctx.emit("task_assigned", "coordinator", target="shipment-agent", decision_code="TIMELINE")
        shipment = await ctx.call("shipment-agent", "get_shipment_summary", order_id=resolved_id)
        if shipment and isinstance(shipment.data, dict):
            attach_shipment(findings, shipment.data)
        ctx.emit("handoff", "shipment-agent", target="coordinator",
                 decision_code=shipment_verdict(findings, detect_issue(findings)).upper(),
                 evidence_refs=ctx.refs_of("shipment-agent"))

    issue = detect_issue(findings, topic)
    ambiguous = len(detect_issues(findings)) > 1

    # --- Policy agent ----------------------------------------------------------------------
    ctx.emit("task_assigned", "coordinator", target="policy-agent", decision_code="APPLY_POLICY")
    rule: dict[str, Any] = {}
    policy_version = case.get("policy_version")
    if policy_version:
        policy = await ctx.call("policy-agent", "get_policy", policy_version=policy_version)
        if policy and isinstance(policy.data, dict):
            rule = (policy.data.get("rules") or {}).get(issue) or {}
    ctx.emit("policy_decided", "policy-agent", decision_code=issue,
             attributes={"case_status": rule.get("case_status"),
                         "action": rule.get("recommended_action")})
    ctx.emit("handoff", "policy-agent", target="coordinator", decision_code=issue.upper(),
             evidence_refs=ctx.refs_of("policy-agent"))

    # --- Conflict resolver -----------------------------------------------------------------
    ctx.emit("task_assigned", "coordinator", target="conflict-resolver", decision_code="SOURCES")
    conflicts = _conflicts(order_row, findings)
    for conflict in conflicts:
        ctx.emit("policy_decided", "conflict-resolver", decision_code=conflict["resolution_code"],
                 attributes={"field": conflict["field"],
                             "selected_source": conflict["selected_source"]})
    ctx.emit("handoff", "conflict-resolver", target="coordinator",
             attributes={"conflicts": len(conflicts)})

    output = _compose(ctx, findings, issue, topic, rule, rejected, customer_unique_id,
                      history_rows, conflicts, ambiguous)

    # --- Verifier --------------------------------------------------------------------------
    ctx.emit("task_assigned", "coordinator", target="verifier", decision_code="VERIFY")
    fixes = _verify(ctx, output)
    ctx.emit("verification_completed", "verifier", decision_code="PASS" if not fixes else "FIXED",
             evidence_refs=output["evidence_refs"][:20] or None,
             attributes={"fixes": len(fixes), "mcp_calls": ctx.call_count})
    ctx.emit("handoff", "verifier", target="coordinator", decision_code="VERIFIED")
    return output


def _debug_dump(ctx: CaseContext) -> None:
    """Optional local dump of consumed evidence (L3B_DEBUG_DIR); never part of the submission."""
    target = os.environ.get("L3B_DEBUG_DIR")
    if not target:
        return
    path = Path(target) / f"{ctx.case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    dump = {ev.tool: ev.data for ev in ctx.ledger.values()}
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")


def _conflicts(order_row: dict[str, Any] | None, f: Findings) -> list[dict[str, Any]]:
    if not order_row or order_row is f.version:
        return []
    conflicts = []
    for name in CONFLICT_FIELDS:
        if order_row.get(name) != f.version.get(name):
            conflicts.append(
                {
                    "field": name,
                    "sources": ["get_order", "get_customer_history"],
                    "selected_source": "get_customer_history",
                    "resolution_code": "LATEST_RECORD_BEFORE_CASE_OPENED",
                }
            )
    return conflicts


def _refund_amount(rule: dict[str, Any], f: Findings) -> float:
    amount = money(rule.get("refund_brl"))
    refundable = max(0.0, f.captured_total - f.refunded_total)
    if f.has_payment_evidence:
        amount = min(amount, refundable)
    return round(max(0.0, amount), 2)


def _responsible(rule: dict[str, Any], f: Findings, issue: str) -> list[dict[str, Any]]:
    parties = []
    for party in rule.get("responsible_parties") or []:
        party_type = party.get("party_type") or "unknown"
        party_id = party.get("party_id")
        if party_type == "seller":
            sellers = f.late_seller_ids if issue == "late_delivery_seller" else []
            sellers = sellers or f.seller_ids
            for seller in sellers:
                parties.append({"party_type": "seller", "party_id": seller})
            continue
        parties.append({"party_type": party_type, "party_id": party_id})
    return parties[:5] or [{"party_type": "unknown", "party_id": None}]


def _compose(
    ctx: CaseContext,
    f: Findings,
    issue: str,
    topic: str | None,
    rule: dict[str, Any],
    rejected: list[str],
    customer_unique_id: str | None,
    history_rows: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    ambiguous: bool,
) -> dict[str, Any]:
    refund = _refund_amount(rule, f) if rule else 0.0
    action = rule.get("recommended_action")
    case_status = rule.get("case_status") or ("no_action" if issue in NO_CLAIM_ISSUES
                                              else "needs_investigation")
    if case_status == "no_action":
        refund = 0.0
    evidence_refs = list(ctx.ledger)[:30]

    matched = topic == issue
    confidence = (0.75 if ambiguous else 0.9) if matched else 0.6
    if not rule:
        confidence = min(confidence, 0.5)

    late_sellers = f.late_seller_ids if issue == "late_delivery_seller" else []
    if issue == "late_delivery_seller" and not late_sellers:
        late_sellers = f.seller_ids

    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [f.order_id],
            "item_ids": f.item_ids[:20],
            "seller_ids": f.seller_ids[:20],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": _claims(ctx, issue, refund, f, evidence_refs),
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": [f.order_id],
            "rejected_candidates": rejected[:20],
            "confidence": 0.95,
        },
        "customer_context": {
            "customer_unique_id": customer_unique_id,
            "related_order_ids": unique([r.get("order_id") for r in history_rows])[:20],
        },
        "shipment_analysis": {
            "verdict": shipment_verdict(f, issue),
            "late_seller_ids": late_sellers[:20],
            "timeline_complete": f.timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict(f, issue),
            "captured_total_brl": _captured(f, issue) if f.has_payment_evidence else None,
            "refunded_total_brl": f.refunded_total if f.has_payment_evidence else None,
            "refundable_total_brl": (
                round(max(0.0, _captured(f, issue) - f.refunded_total), 2)
                if f.has_payment_evidence
                else None
            ),
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": _responsible(rule, f, issue),
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": (
                [{"reason_code": action or issue, "amount_brl": refund, "entity_id": f.order_id}]
                if refund > 0
                else []
            ),
        },
        "resolution_actions": [action] if action else ["request_more_information"],
    }
    return output


def _captured(f: Findings, issue: str) -> float:
    """Captured total of the payments the issue is about (a split excludes unrelated captures)."""
    if issue == "valid_split_payment" and (subset := f.split_subset):
        return round(sum(subset), 2)
    return f.captured_total


def _claims(
    ctx: CaseContext, issue: str, refund: float, f: Findings, refs: list[str]
) -> list[dict[str, Any]]:
    assessments = []
    for claim in (ctx.case.get("customer_request") or {}).get("claims") or []:
        claim_id, topic = claim.get("claim_id"), claim.get("topic")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        if topic == "requested_full_refund":
            if refund <= 0:
                verdict = "unsupported"
            elif f.captured_total and refund >= f.captured_total - 0.01:
                verdict = "supported"
            else:
                verdict = "partially_supported"
        elif topic == issue and issue not in NO_CLAIM_ISSUES:
            verdict = "supported"
        else:
            verdict = "unsupported"
        assessments.append(
            {"claim_id": claim_id[:64], "verdict": verdict, "confidence": 0.85,
             "evidence_refs": refs}
        )
    return assessments[:5]


def _verify(ctx: CaseContext, output: dict[str, Any]) -> list[str]:
    """Enforce cross-field invariants in place; returns the list of applied fixes."""
    fixes: list[str] = []
    refs = [r for r in output["evidence_refs"] if ctx.owns(r)]
    if refs != output["evidence_refs"]:
        output["evidence_refs"] = refs
        fixes.append("foreign_refs")
    for claim in output.get("claim_assessments", []):
        claim["evidence_refs"] = [r for r in claim["evidence_refs"] if r in refs]

    fin = output["financial_resolution"]
    status = output["assessment"]["case_status"]
    if status == "no_action" and fin["recommended_refund_brl"] > 0:
        fin["recommended_refund_brl"], fin["refund_lines"] = 0.0, []
        fixes.append("no_action_refund")
    total = round(sum(line["amount_brl"] for line in fin["refund_lines"]), 2)
    if total != fin["recommended_refund_brl"]:
        fin["recommended_refund_brl"] = total
        fixes.append("refund_sum")
    refundable = output["payment_analysis"]["refundable_total_brl"]
    if refundable is not None and fin["recommended_refund_brl"] > refundable + 0.01:
        fin["recommended_refund_brl"] = refundable
        fin["refund_lines"] = fin["refund_lines"][:1]
        if fin["refund_lines"]:
            fin["refund_lines"][0]["amount_brl"] = refundable
        fixes.append("refund_cap")

    issue = output["assessment"]["primary_issue"]
    shipment = output["shipment_analysis"]
    if issue != "late_delivery_seller" and shipment["late_seller_ids"]:
        shipment["late_seller_ids"] = []
        fixes.append("late_sellers")
    if issue == "late_delivery_logistics":
        parties = output["root_cause_analysis"]["responsible_parties"]
        output["root_cause_analysis"]["responsible_parties"] = [
            p for p in parties if p["party_type"] != "seller"
        ] or [{"party_type": "logistics_provider", "party_id": None}]

    resolved = set(output["entity_resolution"]["resolved_order_ids"])
    rejected = output["entity_resolution"]["rejected_candidates"]
    if resolved & set(rejected):
        output["entity_resolution"]["rejected_candidates"] = [
            c for c in rejected if c not in resolved
        ]
        fixes.append("rejected_overlap")
    output["resolution_actions"] = unique(output["resolution_actions"])[:8]
    return fixes


def _fallback(
    ctx: CaseContext,
    reason: str,
    *,
    rejected: list[str] | None = None,
    customer: str | None = None,
) -> dict[str, Any]:
    """Schema-valid, conservative output built only from evidence this case really obtained."""
    case = ctx.case
    refs = list(ctx.ledger)[:30]
    try:
        ctx.emit("task_assigned", "coordinator", target="verifier", decision_code="FALLBACK")
        ctx.emit("policy_decided", "coordinator", decision_code="insufficient_evidence")
        ctx.emit("verification_completed", "verifier", decision_code=f"FALLBACK_{reason}"[:80],
                 evidence_refs=refs[:20] or None)
        ctx.emit("handoff", "verifier", target="coordinator", decision_code="FALLBACK")
    except Exception:
        pass
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.4,
        },
        "affected_entities": {
            "order_ids": [], "item_ids": [], "seller_ids": [],
            "payment_references": [], "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "not_found" if rejected is not None else "ambiguous",
            "resolved_order_ids": [],
            "rejected_candidates": unique(rejected if rejected is not None else [])[:20],
            "confidence": 0.3,
        },
        "customer_context": {
            "customer_unique_id": customer if isinstance(customer, str) else None,
            "related_order_ids": [],
        },
        "shipment_analysis": {
            "verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None, "refunded_total_brl": None, "refundable_total_brl": None,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0.0,
                                 "refund_lines": []},
        "resolution_actions": ["request_more_information"],
    }
