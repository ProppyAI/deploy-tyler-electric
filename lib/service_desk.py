#!/usr/bin/env python3
"""Service desk — managed queue for setup change requests needing human attention."""

import json
import os
import sys
import time
import uuid

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SERVICE_DESK_DIR = ".harness/service-desk"


def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    tmp_path = path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(desk_dir):
    os.makedirs(desk_dir, exist_ok=True)
    try:
        os.chmod(desk_dir, 0o700)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Ticket CRUD
# ---------------------------------------------------------------------------

def create_ticket(source_message_id, client, requester, channel, request,
                  priority="normal", category="other", desk_dir=_SERVICE_DESK_DIR):
    """Create a service desk ticket. Returns the ticket dict."""
    _ensure_dir(desk_dir)

    ticket_id = str(uuid.uuid4())
    now = _now_iso()

    ticket = {
        "id": ticket_id,
        "source_message_id": source_message_id,
        "client": client,
        "requester": requester,
        "channel": channel,
        "request": request,
        "priority": priority,
        "status": "open",
        "category": category,
        "notes": [],
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
    }

    path = os.path.join(desk_dir, f"{ticket_id}.json")
    save_json(path, ticket)
    return ticket


def get_ticket(ticket_id, desk_dir=_SERVICE_DESK_DIR):
    """Get a single ticket by ID."""
    path = os.path.join(desk_dir, f"{ticket_id}.json")
    if not os.path.isfile(path):
        return None
    try:
        return load_json(path)
    except (json.JSONDecodeError, IOError):
        return None


def list_tickets(status=None, priority=None, client=None, desk_dir=_SERVICE_DESK_DIR):
    """List tickets with optional filters. Returns list of ticket dicts."""
    if not os.path.isdir(desk_dir):
        return []

    tickets = []
    for fname in os.listdir(desk_dir):
        if not fname.endswith(".json"):
            continue
        try:
            ticket = load_json(os.path.join(desk_dir, fname))
        except (json.JSONDecodeError, IOError):
            continue

        if status and ticket.get("status") != status:
            continue
        if priority and ticket.get("priority") != priority:
            continue
        if client and ticket.get("client") != client:
            continue

        tickets.append(ticket)

    # Sort by priority (urgent > high > normal), then by created_at
    priority_order = {"urgent": 0, "high": 1, "normal": 2}
    tickets.sort(key=lambda t: (
        priority_order.get(t.get("priority", "normal"), 9),
        t.get("created_at", ""),
    ))

    return tickets


def update_ticket(ticket_id, status=None, notes=None, desk_dir=_SERVICE_DESK_DIR):
    """Update ticket status or add notes. Returns updated ticket or None."""
    path = os.path.join(desk_dir, f"{ticket_id}.json")
    if not os.path.isfile(path):
        return None

    try:
        ticket = load_json(path)
    except (json.JSONDecodeError, IOError):
        return None

    if status:
        ticket["status"] = status
        if status in ("resolved", "closed"):
            ticket["resolved_at"] = _now_iso()

    if notes:
        ticket["notes"].append({
            "text": notes,
            "added_at": _now_iso(),
        })

    ticket["updated_at"] = _now_iso()
    save_json(path, ticket)
    return ticket


# ---------------------------------------------------------------------------
# CLI display
# ---------------------------------------------------------------------------

def _print_tickets(tickets):
    """Print tickets in a formatted table."""
    if not tickets:
        print("  No tickets found.")
        return

    print(f"  {'ID':<10}{'CLIENT':<20}{'REQUESTER':<16}{'PRIORITY':<10}{'STATUS':<14}{'REQUEST'}")
    print(f"  {'─' * 100}")

    for t in tickets:
        tid = t.get("id", "?")[:8]
        client = t.get("client", "?")[:18]
        requester = t.get("requester", "?")[:14]
        priority = t.get("priority", "?")
        status = t.get("status", "?")
        request = t.get("request", "")[:30]
        print(f"  {tid:<10}{client:<20}{requester:<16}{priority:<10}{status:<14}{request}")

    open_count = sum(1 for t in tickets if t.get("status") == "open")
    in_progress = sum(1 for t in tickets if t.get("status") == "in-progress")
    print(f"\n  {len(tickets)} ticket(s). {open_count} open, {in_progress} in-progress.")


def _print_ticket_detail(ticket):
    """Print a single ticket with full detail."""
    print(f"  ID:         {ticket['id']}")
    print(f"  Client:     {ticket.get('client', '?')}")
    print(f"  Requester:  {ticket.get('requester', '?')}")
    print(f"  Channel:    {ticket.get('channel', '?')}")
    print(f"  Priority:   {ticket.get('priority', '?')}")
    print(f"  Status:     {ticket.get('status', '?')}")
    print(f"  Category:   {ticket.get('category', '?')}")
    print(f"  Request:    {ticket.get('request', '?')}")
    print(f"  Created:    {ticket.get('created_at', '?')}")
    print(f"  Updated:    {ticket.get('updated_at', '?')}")
    if ticket.get("resolved_at"):
        print(f"  Resolved:   {ticket['resolved_at']}")
    if ticket.get("source_message_id"):
        print(f"  Source msg: {ticket['source_message_id'][:8]}")
    if ticket.get("notes"):
        print(f"\n  Notes:")
        for note in ticket["notes"]:
            print(f"    [{note.get('added_at', '?')}] {note.get('text', '')}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="HARNESS Service Desk — managed queue for setup change requests"
    )
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", help="List tickets")
    p_list.add_argument("--status", choices=["open", "in-progress", "resolved", "closed"])
    p_list.add_argument("--priority", choices=["normal", "high", "urgent"])
    p_list.add_argument("--client")
    p_list.add_argument("--desk-dir", default=_SERVICE_DESK_DIR)

    # get
    p_get = sub.add_parser("get", help="Get ticket details")
    p_get.add_argument("ticket_id")
    p_get.add_argument("--desk-dir", default=_SERVICE_DESK_DIR)

    # create
    p_create = sub.add_parser("create", help="Create a ticket")
    p_create.add_argument("--client", required=True)
    p_create.add_argument("--requester", required=True)
    p_create.add_argument("--channel", required=True)
    p_create.add_argument("--request", required=True)
    p_create.add_argument("--priority", default="normal", choices=["normal", "high", "urgent"])
    p_create.add_argument("--category", default="other",
                          choices=["config-change", "feature-request", "complaint", "other"])
    p_create.add_argument("--desk-dir", default=_SERVICE_DESK_DIR)

    # update
    p_update = sub.add_parser("update", help="Update a ticket")
    p_update.add_argument("ticket_id")
    p_update.add_argument("--status", choices=["open", "in-progress", "resolved", "closed"])
    p_update.add_argument("--note")
    p_update.add_argument("--desk-dir", default=_SERVICE_DESK_DIR)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(2)

    if args.command == "list":
        print("HARNESS — Service Desk\n")
        tickets = list_tickets(
            status=args.status, priority=args.priority,
            client=args.client, desk_dir=args.desk_dir,
        )
        _print_tickets(tickets)

    elif args.command == "get":
        ticket = get_ticket(args.ticket_id, desk_dir=args.desk_dir)
        if not ticket:
            print(f"ERROR: Ticket '{args.ticket_id}' not found.", file=sys.stderr)
            sys.exit(1)
        print("HARNESS — Service Desk\n")
        _print_ticket_detail(ticket)

    elif args.command == "create":
        ticket = create_ticket(
            source_message_id="",
            client=args.client,
            requester=args.requester,
            channel=args.channel,
            request=args.request,
            priority=args.priority,
            category=args.category,
            desk_dir=args.desk_dir,
        )
        print(f"HARNESS — Service Desk\n")
        print(f"  Created ticket {ticket['id'][:8]}")
        print(f"  Client:   {ticket['client']}")
        print(f"  Priority: {ticket['priority']}")
        print(f"  Category: {ticket['category']}")
        print(f"  Request:  {ticket['request']}")

    elif args.command == "update":
        if not args.status and not args.note:
            print("ERROR: Provide --status and/or --note", file=sys.stderr)
            sys.exit(2)
        ticket = update_ticket(
            args.ticket_id, status=args.status, notes=args.note,
            desk_dir=args.desk_dir,
        )
        if not ticket:
            print(f"ERROR: Ticket '{args.ticket_id}' not found.", file=sys.stderr)
            sys.exit(1)
        print(f"HARNESS — Service Desk\n")
        print(f"  Updated ticket {ticket['id'][:8]}")
        if args.status:
            print(f"  Status: {args.status}")
        if args.note:
            print(f"  Note added: {args.note}")


if __name__ == "__main__":
    main()
