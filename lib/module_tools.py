#!/usr/bin/env python3
"""
HARNESS Module Tools — JSON-backed CRUD for CRM, estimator, invoicing,
scheduling, and permit-tracker modules.

All data lives in .harness/data/<entity>/<uuid>.json
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA_DIR = ".harness/data"
DEFAULT_MARKUP = 25.0
DEFAULT_TAX_RATE = 8.875


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_data_dir(data_dir, entity):
    """Create .harness/data/<entity>/ if it does not exist and return the path."""
    path = Path(data_dir) / entity
    path.mkdir(parents=True, exist_ok=True)
    return path


def _new_id():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc).isoformat()


def _save(data_dir, entity, record_id, record):
    path = ensure_data_dir(data_dir, entity)
    with open(path / f"{record_id}.json", "w") as f:
        json.dump(record, f, indent=2)
    return record


def _load(data_dir, entity, record_id):
    path = Path(data_dir) / entity / f"{record_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"{entity}/{record_id} not found")
    with open(path) as f:
        return json.load(f)


def _list_all(data_dir, entity):
    path = Path(data_dir) / entity
    if not path.exists():
        return []
    records = []
    for fp in sorted(path.glob("*.json")):
        with open(fp) as f:
            records.append(json.load(f))
    return records


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------

def create_client(name, phone, email, address=None, data_dir=DEFAULT_DATA_DIR):
    client_id = _new_id()
    record = {
        "client_id": client_id,
        "name": name,
        "phone": phone,
        "email": email,
        "address": address or "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    _save(data_dir, "clients", client_id, record)
    return record


def get_client(client_id, data_dir=DEFAULT_DATA_DIR):
    return _load(data_dir, "clients", client_id)


def list_clients(data_dir=DEFAULT_DATA_DIR):
    return _list_all(data_dir, "clients")


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------

def create_estimate(client_id, description, line_items,
                    markup_percentage=DEFAULT_MARKUP,
                    tax_rate=DEFAULT_TAX_RATE,
                    data_dir=DEFAULT_DATA_DIR):
    estimate_id = _new_id()

    # Compute line item totals
    computed_items = []
    subtotal = 0.0
    for item in line_items:
        qty = float(item["quantity"])
        unit_price = float(item["unit_price"])
        total = round(qty * unit_price, 2)
        computed_items.append({
            "description": item["description"],
            "quantity": qty,
            "unit_price": unit_price,
            "total": total,
        })
        subtotal += total

    markup_amount = round(subtotal * (markup_percentage / 100.0), 2)
    subtotal_with_markup = round(subtotal + markup_amount, 2)
    tax_amount = round(subtotal_with_markup * (tax_rate / 100.0), 2)
    grand_total = round(subtotal_with_markup + tax_amount, 2)

    record = {
        "estimate_id": estimate_id,
        "client_id": client_id,
        "description": description,
        "line_items": computed_items,
        "subtotal": subtotal,
        "markup_percentage": markup_percentage,
        "markup_amount": markup_amount,
        "subtotal_with_markup": subtotal_with_markup,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "grand_total": grand_total,
        "status": "draft",
        "created_at": _now(),
        "updated_at": _now(),
    }
    _save(data_dir, "estimates", estimate_id, record)
    return record


def get_estimate(estimate_id, data_dir=DEFAULT_DATA_DIR):
    return _load(data_dir, "estimates", estimate_id)


def list_estimates(client_id=None, data_dir=DEFAULT_DATA_DIR):
    records = _list_all(data_dir, "estimates")
    if client_id:
        records = [r for r in records if r.get("client_id") == client_id]
    return records


# ---------------------------------------------------------------------------
# Invoicing
# ---------------------------------------------------------------------------

def create_invoice(estimate_id, data_dir=DEFAULT_DATA_DIR):
    estimate = get_estimate(estimate_id, data_dir=data_dir)
    invoice_id = _new_id()
    record = {
        "invoice_id": invoice_id,
        "estimate_id": estimate_id,
        "client_id": estimate["client_id"],
        "amount": estimate["grand_total"],
        "amount_paid": 0.0,
        "balance": estimate["grand_total"],
        "status": "draft",
        "payments": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    _save(data_dir, "invoices", invoice_id, record)
    return record


def send_invoice(invoice_id, data_dir=DEFAULT_DATA_DIR):
    record = _load(data_dir, "invoices", invoice_id)
    record["status"] = "sent"
    record["sent_at"] = _now()
    record["updated_at"] = _now()
    _save(data_dir, "invoices", invoice_id, record)
    return record


def record_payment(invoice_id, amount, method, data_dir=DEFAULT_DATA_DIR):
    record = _load(data_dir, "invoices", invoice_id)
    amount = float(amount)
    payment = {
        "payment_id": _new_id(),
        "amount": amount,
        "method": method,
        "recorded_at": _now(),
    }
    record["payments"].append(payment)
    record["amount_paid"] = round(record["amount_paid"] + amount, 2)
    record["balance"] = round(record["amount"] - record["amount_paid"], 2)
    if record["balance"] <= 0:
        record["status"] = "paid"
        record["balance"] = 0.0
    else:
        record["status"] = "partial"
    record["updated_at"] = _now()
    _save(data_dir, "invoices", invoice_id, record)
    return record


def list_invoices(client_id=None, status=None, data_dir=DEFAULT_DATA_DIR):
    records = _list_all(data_dir, "invoices")
    if client_id:
        records = [r for r in records if r.get("client_id") == client_id]
    if status:
        records = [r for r in records if r.get("status") == status]
    return records


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def create_schedule(job_id, crew, date, time, data_dir=DEFAULT_DATA_DIR):
    schedule_id = _new_id()
    record = {
        "schedule_id": schedule_id,
        "job_id": job_id,
        "crew": crew,
        "date": date,
        "time": time,
        "status": "scheduled",
        "created_at": _now(),
        "updated_at": _now(),
    }
    _save(data_dir, "schedules", schedule_id, record)
    return record


def list_schedule(date=None, data_dir=DEFAULT_DATA_DIR):
    records = _list_all(data_dir, "schedules")
    if date:
        records = [r for r in records if r.get("date") == date]
    return records


# ---------------------------------------------------------------------------
# Permit Tracker
# ---------------------------------------------------------------------------

def file_permit(job_id, jurisdiction, permit_type, data_dir=DEFAULT_DATA_DIR):
    permit_id = _new_id()
    record = {
        "permit_id": permit_id,
        "job_id": job_id,
        "jurisdiction": jurisdiction,
        "permit_type": permit_type,
        "status": "filed",
        "filed_at": _now(),
        "created_at": _now(),
        "updated_at": _now(),
    }
    _save(data_dir, "permits", permit_id, record)
    return record


def check_permit_status(permit_id, data_dir=DEFAULT_DATA_DIR):
    return _load(data_dir, "permits", permit_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="HARNESS module tool executor"
    )
    sub = parser.add_subparsers(dest="command")

    def _add_data_dir(p):
        p.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="Data directory (default: .harness/data)")
        return p

    # -- create-client
    p = _add_data_dir(sub.add_parser("create-client"))
    p.add_argument("--name", required=True)
    p.add_argument("--phone", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--address", default="")

    # -- get-client
    p = _add_data_dir(sub.add_parser("get-client"))
    p.add_argument("--client-id", required=True)

    # -- list-clients
    _add_data_dir(sub.add_parser("list-clients"))

    # -- create-estimate
    p = _add_data_dir(sub.add_parser("create-estimate"))
    p.add_argument("--client-id", required=True)
    p.add_argument("--description", required=True)
    p.add_argument("--items", required=True,
                   help="JSON array of line items")
    p.add_argument("--markup", type=float, default=DEFAULT_MARKUP)
    p.add_argument("--tax-rate", type=float, default=DEFAULT_TAX_RATE)

    # -- get-estimate
    p = _add_data_dir(sub.add_parser("get-estimate"))
    p.add_argument("--estimate-id", required=True)

    # -- list-estimates
    p = _add_data_dir(sub.add_parser("list-estimates"))
    p.add_argument("--client-id", default=None)

    # -- create-invoice
    p = _add_data_dir(sub.add_parser("create-invoice"))
    p.add_argument("--estimate-id", required=True)

    # -- send-invoice
    p = _add_data_dir(sub.add_parser("send-invoice"))
    p.add_argument("--invoice-id", required=True)

    # -- record-payment
    p = _add_data_dir(sub.add_parser("record-payment"))
    p.add_argument("--invoice-id", required=True)
    p.add_argument("--amount", required=True, type=float)
    p.add_argument("--method", required=True)

    # -- list-invoices
    p = _add_data_dir(sub.add_parser("list-invoices"))
    p.add_argument("--client-id", default=None)
    p.add_argument("--status", default=None)

    # -- create-schedule
    p = _add_data_dir(sub.add_parser("create-schedule"))
    p.add_argument("--job-id", required=True)
    p.add_argument("--crew", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--time", required=True)

    # -- list-schedule
    p = _add_data_dir(sub.add_parser("list-schedule"))
    p.add_argument("--date", default=None)

    # -- file-permit
    p = _add_data_dir(sub.add_parser("file-permit"))
    p.add_argument("--job-id", required=True)
    p.add_argument("--jurisdiction", required=True)
    p.add_argument("--permit-type", required=True)

    # -- check-permit-status
    p = _add_data_dir(sub.add_parser("check-permit-status"))
    p.add_argument("--permit-id", required=True)

    args = parser.parse_args()
    data_dir = getattr(args, "data_dir", DEFAULT_DATA_DIR)

    if args.command == "create-client":
        result = create_client(args.name, args.phone, args.email,
                               address=args.address, data_dir=data_dir)
    elif args.command == "get-client":
        result = get_client(args.client_id, data_dir=data_dir)
    elif args.command == "list-clients":
        result = list_clients(data_dir=data_dir)
    elif args.command == "create-estimate":
        items = json.loads(args.items)
        result = create_estimate(args.client_id, args.description, items,
                                 markup_percentage=args.markup,
                                 tax_rate=args.tax_rate,
                                 data_dir=data_dir)
    elif args.command == "get-estimate":
        result = get_estimate(args.estimate_id, data_dir=data_dir)
    elif args.command == "list-estimates":
        result = list_estimates(client_id=args.client_id, data_dir=data_dir)
    elif args.command == "create-invoice":
        result = create_invoice(args.estimate_id, data_dir=data_dir)
    elif args.command == "send-invoice":
        result = send_invoice(args.invoice_id, data_dir=data_dir)
    elif args.command == "record-payment":
        result = record_payment(args.invoice_id, args.amount, args.method,
                                data_dir=data_dir)
    elif args.command == "list-invoices":
        result = list_invoices(client_id=args.client_id, status=args.status,
                               data_dir=data_dir)
    elif args.command == "create-schedule":
        result = create_schedule(args.job_id, args.crew, args.date, args.time,
                                 data_dir=data_dir)
    elif args.command == "list-schedule":
        result = list_schedule(date=args.date, data_dir=data_dir)
    elif args.command == "file-permit":
        result = file_permit(args.job_id, args.jurisdiction, args.permit_type,
                             data_dir=data_dir)
    elif args.command == "check-permit-status":
        result = check_permit_status(args.permit_id, data_dir=data_dir)
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
