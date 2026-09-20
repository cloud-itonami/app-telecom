#!/usr/bin/env python3
"""Zeebe worker for telecom Phase 1 (eTOM Customer + Service Provisioning).

Six task types serve the BPMN actors registered in
``20260427090100_seed_telecom_bpmn_actors.ts``:

- ``telecom.subscriber.onboard``  — creates subscriber + Tier-3 PII split
- ``telecom.sim.activate``        — binds an ICCID to a subscriber
- ``telecom.service.provision``   — opens a service instance with QoS profile
- ``telecom.usage.record``        — appends a CDR row
- ``telecom.billing.cycle``       — aggregates CDRs over a period -> invoice
- ``telecom.payment.record``      — records a payment against an invoice
- ``telecom.payment.refund``       — records a refund (credit memo) against an invoice
- ``telecom.sla.escalate``        — opens an SLA breach + ticket id

The worker only writes graph rows; AT Repo dispatch / federation is left to
upstream callers per ADR-0036 (Worker-direct Hyperdrive). PII (raw MSISDN /
IMSI / customer name) is stored Tier-3 per ADR-0018 — public AT Repo holds
hashed identifiers only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # noqa: BLE001
    psycopg = None
    dict_row = None


AGENTGATEWAY_MCP_URL = os.environ.get(
    "AGENTGATEWAY_MCP_URL",
    "http://agentgateway-mcp.mitama-udf.svc.cluster.local:8080",
)
RW_URL = os.environ.get("RW_URL") or os.environ.get("DATABASE_URL")
DEFAULT_CALLER_DID = "did:web:telecom.etzhayyim.com"
ACTOR_TAG = "sys.worker.telecom"


async def serve_langserver(tasks: dict[str, Callable[..., Any]]) -> None:
    port = int(os.environ.get("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._json(200, {"ok": True, "runtimeKind": "k8s-langserver", "agentGatewayMcpUrl": AGENTGATEWAY_MCP_URL})
            elif self.path == "/tools":
                self._json(200, {"tools": [{"name": name, "runtime": "langserver"} for name in sorted(tasks)]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            name = str(payload.get("name") or payload.get("tool") or payload.get("assistant_id") or "")
            arguments = payload.get("arguments") or payload.get("input") or {}
            handler = tasks.get(name)
            if handler is None:
                self._json(404, {"error": f"unknown tool: {name}"})
                return
            result = asyncio.run(handler(**arguments))
            self._json(200, {"ok": True, "name": name, "result": result})

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    await asyncio.to_thread(server.serve_forever)


def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_id(prefix: str, *parts: object) -> str:
    if parts:
        digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:24]
        return f"{prefix}_{digest}"
    return f"{prefix}_{secrets.token_urlsafe(16).replace('-', '').replace('_', '')[:20]}"


def hash_identifier(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_date(value: object, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return date.fromisoformat(value[:10])


def require(payload: dict[str, Any], fields: list[str]) -> None:
    missing = [field for field in fields if not str(payload.get(field, "")).strip()]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")


def caller_did(payload: dict[str, Any]) -> str:
    return str(payload.get("callerDid") or DEFAULT_CALLER_DID)


def base_audit(payload: dict[str, Any]) -> dict[str, Any]:
    did = caller_did(payload)
    return {
        "created_at": now_iso(),
        "sensitivity_ord": 2,
        "org_id": did,
        "user_id": did,
        "actor_id": ACTOR_TAG,
    }


class GraphConnection:
    def __init__(self, url: str):
        if psycopg is None or dict_row is None:
            raise RuntimeError("RW_URL is set but psycopg is not installed")
        self._con = psycopg.connect(url, row_factory=dict_row)

    def __enter__(self) -> "GraphConnection":
        self._con.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._con.__exit__(exc_type, exc, tb)

    def execute(self, query: str, params: Iterable[Any] | dict[str, Any] | None = None) -> Any:
        if isinstance(params, dict):
            query = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", query)
        elif params is not None:
            query = query.replace("?", "%s")
        return self._con.execute(query, params)


def maybe_insert(table: str, row: dict[str, Any]) -> None:
    if not RW_URL:
        return
    columns = list(row)
    placeholders = ", ".join(f":{column}" for column in columns)
    names = ", ".join(columns)
    with GraphConnection(str(RW_URL)) as con:
        con.execute(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",  # noqa: S608
            row,
        )


def fetch_cdr_aggregates(subscriber_vid: str, period_start: date, period_end: date) -> dict[str, float]:
    totals = {"voice": 0.0, "sms": 0.0, "data": 0.0, "iot": 0.0}
    if not RW_URL:
        return totals
    with GraphConnection(str(RW_URL)) as con:
        cur = con.execute(
            """
            SELECT usage_type, SUM(units) AS units
            FROM vertex_telecom_cdr
            WHERE subscriber_vid = :svid
              AND started_at >= :ps
              AND started_at <  :pe
              AND status = 'recorded'
            GROUP BY usage_type
            """,
            {
                "svid": subscriber_vid,
                "ps": period_start.isoformat(),
                "pe": period_end.isoformat(),
            },
        )
        for row in cur.fetchall():
            usage_type = str(row["usage_type"])
            units = float(row["units"] or 0.0)
            totals[usage_type] = totals.get(usage_type, 0.0) + units
    return totals


# ---------- handlers ----------


def onboard_payload(payload: dict[str, Any]) -> dict[str, Any]:
    require(payload, ["customerName", "msisdn", "kycStatus", "planId"])
    msisdn = str(payload["msisdn"])
    imsi = payload.get("imsi")
    subscriber_id = str(payload.get("subscriberId") or new_id("sub", msisdn))
    vertex_id = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.subscriber/{subscriber_id}"
    audit = base_audit(payload)
    now = now_iso()

    main_row = {
        "vertex_id": vertex_id,
        "owner_did": caller_did(payload),
        "subscriber_id": subscriber_id,
        "msisdn_hash": hash_identifier(msisdn),
        "imsi_hash": hash_identifier(imsi),
        "kyc_status": str(payload["kycStatus"]),
        "plan_id": str(payload["planId"]),
        "status": "active" if str(payload["kycStatus"]) == "verified" else "pending",
        "onboarded_at": now,
        **audit,
    }
    maybe_insert("vertex_telecom_subscriber", main_row)

    pii_row = {
        "vertex_id": f"{vertex_id}/pii",
        "owner_did": caller_did(payload),
        "subscriber_vid": vertex_id,
        "customer_name": str(payload["customerName"]),
        "msisdn": msisdn,
        "imsi": str(imsi) if imsi else None,
        **audit,
        "sensitivity_ord": 3,
    }
    maybe_insert("vertex_telecom_subscriber_pii", pii_row)

    return {
        "ok": True,
        "vertexId": vertex_id,
        "subscriberId": subscriber_id,
        "status": main_row["status"],
    }


async def telecom_subscriber_onboard(payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return onboard_payload(dict(payload or kwargs))


def activate_sim_payload(payload: dict[str, Any]) -> dict[str, Any]:
    require(payload, ["iccid", "subscriberId"])
    iccid = str(payload["iccid"])
    subscriber_id = str(payload["subscriberId"])
    sim_id = str(payload.get("simId") or new_id("sim", iccid))
    subscriber_vid = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.subscriber/{subscriber_id}"
    vertex_id = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.sim/{sim_id}"
    row = {
        "vertex_id": vertex_id,
        "owner_did": caller_did(payload),
        "sim_id": sim_id,
        "iccid_hash": hash_identifier(iccid),
        "subscriber_vid": subscriber_vid,
        "sim_type": str(payload.get("simType") or "physical"),
        "status": "active",
        "activated_at": now_iso(),
        **base_audit(payload),
    }
    maybe_insert("vertex_telecom_sim", row)
    return {"ok": True, "vertexId": vertex_id, "simId": sim_id, "status": row["status"]}


async def telecom_sim_activate(payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return activate_sim_payload(dict(payload or kwargs))


def provision_service_payload(payload: dict[str, Any]) -> dict[str, Any]:
    require(payload, ["subscriberId", "serviceType", "planId"])
    service_type = str(payload["serviceType"])
    if service_type not in {"voice", "sms", "data", "iot", "fixed_line", "fiber"}:
        raise ValueError(f"unsupported serviceType: {service_type}")
    subscriber_id = str(payload["subscriberId"])
    service_id = str(payload.get("serviceId") or new_id("svc", subscriber_id, service_type, payload["planId"]))
    subscriber_vid = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.subscriber/{subscriber_id}"
    vertex_id = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.service/{service_id}"
    row = {
        "vertex_id": vertex_id,
        "owner_did": caller_did(payload),
        "service_id": service_id,
        "subscriber_vid": subscriber_vid,
        "sim_vid": (
            f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.sim/{payload['simId']}"
            if payload.get("simId") else None
        ),
        "service_type": service_type,
        "plan_id": str(payload["planId"]),
        "qos_profile": payload.get("qosProfile"),
        "apn": payload.get("apn"),
        "status": "active",
        "provisioned_at": now_iso(),
        **base_audit(payload),
    }
    maybe_insert("vertex_telecom_service", row)
    return {"ok": True, "vertexId": vertex_id, "serviceId": service_id, "status": row["status"]}


async def telecom_service_provision(payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return provision_service_payload(dict(payload or kwargs))


def record_usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    require(payload, ["subscriberId", "serviceId", "usageType", "units", "startedAt"])
    usage_type = str(payload["usageType"])
    if usage_type not in {"voice", "sms", "data", "iot"}:
        raise ValueError(f"unsupported usageType: {usage_type}")
    units = float(payload["units"])
    if units < 0:
        raise ValueError("units must be non-negative")
    subscriber_id = str(payload["subscriberId"])
    service_id = str(payload["serviceId"])
    cdr_id = str(payload.get("cdrId") or new_id("cdr", subscriber_id, service_id, usage_type, payload["startedAt"]))
    vertex_id = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.cdr/{cdr_id}"
    row = {
        "vertex_id": vertex_id,
        "owner_did": caller_did(payload),
        "cdr_id": cdr_id,
        "subscriber_vid": f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.subscriber/{subscriber_id}",
        "service_vid": f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.service/{service_id}",
        "usage_type": usage_type,
        "units": units,
        "unit_of_measure": payload.get("unitOfMeasure"),
        "peer_msisdn_hash": hash_identifier(payload.get("peerMsisdn")),
        "started_at": str(payload["startedAt"]),
        "ended_at": str(payload["endedAt"]) if payload.get("endedAt") else None,
        "status": "recorded",
        **base_audit(payload),
    }
    maybe_insert("vertex_telecom_cdr", row)
    return {"ok": True, "vertexId": vertex_id, "cdrId": cdr_id, "status": row["status"]}


async def telecom_usage_record(payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return record_usage_payload(dict(payload or kwargs))


# Cents-per-unit; coarse default rate card for Phase 1 invoice math.
RATE_CARD = {
    "voice": 0.02,   # per second
    "sms": 0.05,     # per message
    "data": 0.000_000_01,  # per byte ~ 0.01 per MB
    "iot": 0.001,    # per event
}


def billing_cycle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    require(payload, ["subscriberId", "periodStart", "periodEnd"])
    period_start = parse_date(payload["periodStart"], "periodStart")
    period_end = parse_date(payload["periodEnd"], "periodEnd")
    if period_end <= period_start:
        raise ValueError("periodEnd must be after periodStart")
    subscriber_id = str(payload["subscriberId"])
    subscriber_vid = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.subscriber/{subscriber_id}"
    cycle_id = str(payload.get("cycleId") or f"{period_start.isoformat()}_{period_end.isoformat()}")
    invoice_id = str(payload.get("invoiceId") or new_id("inv", subscriber_id, cycle_id))
    vertex_id = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.invoice/{invoice_id}"

    totals = fetch_cdr_aggregates(subscriber_vid, period_start, period_end)
    # Currency line items per usage type (rate card is cents-per-unit), so the
    # invoice row is reconcilable to its own total without re-running RATE_CARD.
    amounts = {k: float(round(totals.get(k, 0.0) * RATE_CARD[k], 4)) for k in RATE_CARD}
    # Tax line (e.g. JP consumption tax 0.10); default 0.0 keeps legacy totals.
    tax_rate = float(payload.get("taxRate") or 0.0)
    if not 0.0 <= tax_rate <= 1.0:
        raise ValueError("taxRate must be between 0.0 and 1.0")
    tax_amount = float(round(sum(amounts.values()) * tax_rate, 4))
    total_amount = float(round(sum(amounts.values()) + tax_amount, 4))
    row = {
        "vertex_id": vertex_id,
        "owner_did": caller_did(payload),
        "invoice_id": invoice_id,
        "cycle_id": cycle_id,
        "subscriber_vid": subscriber_vid,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "currency": str(payload.get("currency") or "JPY"),
        "total_amount": total_amount,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "amount_voice": amounts["voice"],
        "amount_sms": amounts["sms"],
        "amount_data": amounts["data"],
        "amount_iot": amounts["iot"],
        "voice_units": totals.get("voice", 0.0),
        "sms_units": totals.get("sms", 0.0),
        "data_units": totals.get("data", 0.0),
        "iot_units": totals.get("iot", 0.0),
        "status": "issued",
        **base_audit(payload),
    }
    maybe_insert("vertex_telecom_invoice", row)
    return {
        "ok": True,
        "vertexId": vertex_id,
        "invoiceId": invoice_id,
        "totalAmount": row["total_amount"],
        "taxAmount": row["tax_amount"],
        "units": {
            "voice": row["voice_units"],
            "sms": row["sms_units"],
            "data": row["data_units"],
            "iot": row["iot_units"],
        },
        "amounts": {
            "voice": row["amount_voice"],
            "sms": row["amount_sms"],
            "data": row["amount_data"],
            "iot": row["amount_iot"],
        },
        "status": row["status"],
    }


async def telecom_billing_cycle(payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return billing_cycle_payload(dict(payload or kwargs))


def payment_record_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Record a payment against an invoice (eTOM billing/charging collection).

    Idempotent on ``paymentId``: re-recording an existing payment returns the
    original result without inserting a second row or touching the invoice
    again. Only an ``issued`` invoice can be settled; a ``paid`` invoice is
    rejected as a double-settlement.
    """
    require(payload, ["invoiceId", "amount"])
    invoice_id = str(payload["invoiceId"])
    amount = float(payload["amount"])
    if amount <= 0:
        raise ValueError("amount must be positive")
    method = str(payload.get("method") or "bank_transfer")
    if method not in {"bank_transfer", "credit_card", "wallet"}:
        raise ValueError(f"unsupported method: {method}")
    payment_id = str(payload.get("paymentId") or new_id("pay", invoice_id, amount, method))
    vertex_id = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.payment/{payment_id}"
    audit = base_audit(payload)
    now = now_iso()

    if RW_URL:
        with GraphConnection(str(RW_URL)) as con:
            existing = con.execute(
                "SELECT vertex_id, invoice_vid, amount, method, recorded_at, status"
                " FROM vertex_telecom_payment WHERE payment_id = :pid",
                {"pid": payment_id},
            ).fetchone()
            if existing is not None:
                return {
                    "ok": True,
                    "vertexId": existing["vertex_id"],
                    "paymentId": payment_id,
                    "invoiceId": invoice_id,
                    "amount": float(existing["amount"]),
                    "method": str(existing["method"]),
                    "status": str(existing["status"]),
                    "idempotent": True,
                }
            invoice = con.execute(
                "SELECT vertex_id, total_amount, status FROM vertex_telecom_invoice"
                " WHERE invoice_id = :iid",
                {"iid": invoice_id},
            ).fetchone()
            if invoice is None:
                raise ValueError(f"unknown invoiceId: {invoice_id}")
            if invoice["status"] != "issued":
                raise ValueError(f"invoice {invoice_id} is {invoice['status']}, not issuable for payment")
            total = float(invoice["total_amount"] or 0.0)
            if amount > total:
                raise ValueError(
                    f"payment {amount} exceeds invoice {invoice_id} total {total};"
                    " overpayment requires a credit memo, not a payment row"
                )
    else:
        invoice = None

    invoice_vid = (
        invoice["vertex_id"]
        if invoice is not None
        else f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.invoice/{invoice_id}"
    )
    row = {
        "vertex_id": vertex_id,
        "owner_did": caller_did(payload),
        "payment_id": payment_id,
        "invoice_vid": invoice_vid,
        "amount": amount,
        "currency": str(payload.get("currency") or "JPY"),
        "method": method,
        "recorded_at": now,
        "status": "captured",
        **audit,
    }
    maybe_insert("vertex_telecom_payment", row)
    if RW_URL:
        with GraphConnection(str(RW_URL)) as con:
            # Settlement flips the invoice to 'paid' only when recorded payments
            # cover the invoice total; a partial payment leaves it collectible.
            paid_sum = con.execute(
                "SELECT COALESCE(SUM(amount), 0) AS paid FROM vertex_telecom_payment"
                " WHERE invoice_vid = :iv AND status = 'captured'",
                {"iv": invoice_vid},
            ).fetchone()
            paid_total = float(paid_sum["paid"]) if paid_sum is not None else 0.0
            invoice_total = float(invoice["total_amount"]) if invoice is not None else 0.0
            next_status = "paid" if paid_total >= invoice_total else "issued"
            con.execute(
                "UPDATE vertex_telecom_invoice SET status = :st, updated_at = :now"
                " WHERE invoice_id = :iid AND status = 'issued'",
                {"st": next_status, "now": now, "iid": invoice_id},
            )
    return {
        "ok": True,
        "vertexId": vertex_id,
        "paymentId": payment_id,
        "invoiceId": invoice_id,
        "amount": amount,
        "method": method,
        "status": row["status"],
    }


async def telecom_payment_record(payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return payment_record_payload(dict(payload or kwargs))


def payment_refund_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Record a refund (credit memo) against an invoice (eTOM billing/charging collection).

    Landing path for the payment-record overpayment guard: a refund row is
    stored as a negative-amount payment with method ``refund`` and status
    ``captured``, so the settlement coverage sum decreases naturally; an
    invoice that drops below its total flips back to ``issued``
    (collectible again). Idempotent on ``refundId``.
    """
    require(payload, ["invoiceId", "amount"])
    invoice_id = str(payload["invoiceId"])
    amount = float(payload["amount"])
    if amount <= 0:
        raise ValueError("refund amount must be positive")
    refund_id = str(payload.get("refundId") or new_id("ref", invoice_id, amount))
    vertex_id = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.payment/{refund_id}"
    audit = base_audit(payload)
    now = now_iso()

    if RW_URL:
        with GraphConnection(str(RW_URL)) as con:
            existing = con.execute(
                "SELECT vertex_id, amount, status FROM vertex_telecom_payment WHERE payment_id = :pid",
                {"pid": refund_id},
            ).fetchone()
            if existing is not None:
                return {
                    "ok": True,
                    "vertexId": existing["vertex_id"],
                    "refundId": refund_id,
                    "invoiceId": invoice_id,
                    "amount": float(existing["amount"]),
                    "status": str(existing["status"]),
                    "idempotent": True,
                }
            invoice = con.execute(
                "SELECT vertex_id, total_amount FROM vertex_telecom_invoice"
                " WHERE invoice_id = :iid",
                {"iid": invoice_id},
            ).fetchone()
            if invoice is None:
                raise ValueError(f"unknown invoiceId: {invoice_id}")
            captured = con.execute(
                "SELECT COALESCE(SUM(amount), 0) AS net FROM vertex_telecom_payment"
                " WHERE invoice_vid = :iv AND status = 'captured'",
                {"iv": invoice["vertex_id"]},
            ).fetchone()
            net = float(captured["net"]) if captured is not None else 0.0
            if amount > net:
                raise ValueError(
                    f"refund {amount} exceeds net captured payments {net}"
                    f" on invoice {invoice_id}")
    else:
        invoice = None

    invoice_vid = (
        invoice["vertex_id"]
        if invoice is not None
        else f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.invoice/{invoice_id}"
    )
    row = {
        "vertex_id": vertex_id,
        "owner_did": caller_did(payload),
        "payment_id": refund_id,
        "invoice_vid": invoice_vid,
        "amount": -amount,
        "currency": str(payload.get("currency") or "JPY"),
        "method": "refund",
        "recorded_at": now,
        "status": "captured",
        **audit,
    }
    maybe_insert("vertex_telecom_payment", row)
    if RW_URL:
        with GraphConnection(str(RW_URL)) as con:
            paid_sum = con.execute(
                "SELECT COALESCE(SUM(amount), 0) AS paid FROM vertex_telecom_payment"
                " WHERE invoice_vid = :iv AND status = 'captured'",
                {"iv": invoice_vid},
            ).fetchone()
            paid_total = float(paid_sum["paid"]) if paid_sum is not None else 0.0
            invoice_total = float(invoice["total_amount"]) if invoice is not None else 0.0
            next_status = "paid" if (invoice_total > 0 and paid_total >= invoice_total) else "issued"
            con.execute(
                "UPDATE vertex_telecom_invoice SET status = :st, updated_at = :now"
                " WHERE invoice_id = :iid",
                {"st": next_status, "now": now, "iid": invoice_id},
            )
    return {
        "ok": True,
        "vertexId": vertex_id,
        "refundId": refund_id,
        "invoiceId": invoice_id,
        "amount": -amount,
        "method": "refund",
        "status": row["status"],
    }


async def telecom_payment_refund(payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return payment_refund_payload(dict(payload or kwargs))



def sla_escalate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    require(payload, ["serviceId", "breachType", "severity", "observedAt"])
    severity = str(payload["severity"])
    if severity not in {"minor", "major", "critical"}:
        raise ValueError(f"unsupported severity: {severity}")
    service_id = str(payload["serviceId"])
    service_vid = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.service/{service_id}"
    breach_id = str(payload.get("breachId") or new_id("brc", service_id, payload["observedAt"], payload["breachType"]))
    ticket_id = str(payload.get("ticketId") or new_id("tkt", breach_id))
    vertex_id = f"at://did:web:telecom.etzhayyim.com/com.etzhayyim.apps.telecom.slaBreach/{breach_id}"
    observed_value = payload.get("observedValue")
    sla_threshold = payload.get("slaThreshold")
    row = {
        "vertex_id": vertex_id,
        "owner_did": caller_did(payload),
        "breach_id": breach_id,
        "service_vid": service_vid,
        "breach_type": str(payload["breachType"]),
        "severity": severity,
        "metric": payload.get("metric"),
        "observed_value": float(observed_value) if observed_value is not None else None,
        "sla_threshold": float(sla_threshold) if sla_threshold is not None else None,
        "observed_at": str(payload["observedAt"]),
        "ticket_id": ticket_id,
        "status": "open",
        **base_audit(payload),
    }
    maybe_insert("vertex_telecom_sla_breach", row)
    return {
        "ok": True,
        "vertexId": vertex_id,
        "breachId": breach_id,
        "ticketId": ticket_id,
        "status": row["status"],
    }


async def telecom_sla_escalate(payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return sla_escalate_payload(dict(payload or kwargs))


# ---------- runtime ----------


async def serve() -> None:
    await serve_langserver({
        "telecom.subscriber.onboard": telecom_subscriber_onboard,
        "telecom.sim.activate": telecom_sim_activate,
        "telecom.service.provision": telecom_service_provision,
        "telecom.usage.record": telecom_usage_record,
        "telecom.billing.cycle": telecom_billing_cycle,
        "telecom.payment.record": telecom_payment_record,
        "telecom.payment.refund": telecom_payment_refund,
        "telecom.sla.escalate": telecom_sla_escalate,
    })


COMMANDS = {
    "onboard": onboard_payload,
    "activate-sim": activate_sim_payload,
    "provision": provision_service_payload,
    "record-usage": record_usage_payload,
    "billing": billing_cycle_payload,
    "payment": payment_record_payload,
    "refund": payment_refund_payload,
    "escalate": sla_escalate_payload,
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=[*COMMANDS.keys(), "serve", "dry-run"])
    parser.add_argument("payload", nargs="?", default="{}")
    args = parser.parse_args(argv)
    if args.command == "serve":
        asyncio.run(serve())
        return 0
    if args.command == "dry-run":
        sub = onboard_payload({
            "customerName": "Demo User",
            "msisdn": "+819012345678",
            "imsi": "440101234567890",
            "kycStatus": "verified",
            "planId": "demo-mvno-1g",
        })
        sim = activate_sim_payload({
            "iccid": "8981000123456789012",
            "subscriberId": sub["subscriberId"],
            "simType": "esim",
        })
        svc = provision_service_payload({
            "subscriberId": sub["subscriberId"],
            "simId": sim["simId"],
            "serviceType": "data",
            "planId": "demo-mvno-1g",
            "qosProfile": "best_effort",
            "apn": "internet",
        })
        cdr = record_usage_payload({
            "subscriberId": sub["subscriberId"],
            "serviceId": svc["serviceId"],
            "usageType": "data",
            "units": 1_048_576,
            "unitOfMeasure": "bytes",
            "startedAt": now_iso(),
        })
        bill = billing_cycle_payload({
            "subscriberId": sub["subscriberId"],
            "periodStart": today_iso(),
            "periodEnd": "2099-12-31",
        })
        payment = payment_record_payload({
            "invoiceId": bill["invoiceId"],
            "amount": bill["totalAmount"] or 1.0,
            "method": "bank_transfer",
        })
        sla = sla_escalate_payload({
            "serviceId": svc["serviceId"],
            "breachType": "latency",
            "severity": "minor",
            "metric": "p95_ms",
            "observedValue": 250.0,
            "slaThreshold": 100.0,
            "observedAt": now_iso(),
        })
        print(json.dumps(
            {"onboard": sub, "sim": sim, "service": svc, "cdr": cdr, "billing": bill, "payment": payment, "sla": sla},
            ensure_ascii=False, sort_keys=True,
        ))
        return 0
    payload = json.loads(args.payload)
    result = COMMANDS[args.command](payload)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
