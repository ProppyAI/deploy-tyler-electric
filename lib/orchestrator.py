#!/usr/bin/env python3
"""Orchestrator — routes inbox messages to module tools or service desk."""

import json
import os
import re
import sys
import time

# ---------------------------------------------------------------------------
# Helpers (reuse inbox_manager patterns for consistency)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Keyword → module/tool routing table
# ---------------------------------------------------------------------------

_ROUTING_RULES = [
    # (keywords, module, tool)
    (["invoice", "bill", "payment", "pay", "billing", "charge"],
     "ops", "invoicing"),
    (["estimate", "quote", "bid", "proposal"],
     "ops", "estimator"),
    (["schedule", "appointment", "book", "calendar", "reschedule"],
     "ops", "scheduling"),
    (["client", "customer", "contact", "crm"],
     "ops", "crm"),
    (["permit", "inspection", "code compliance"],
     "ops", "permit-tracker"),
]

# Intents that are business tasks (can be auto-handled)
_BUSINESS_INTENTS = {"billing-inquiry", "scheduling", "status-check", "question"}

# Intents that always go to service desk
_SERVICE_DESK_INTENTS = {"feature-request", "complaint", "technical", "unknown"}


# ---------------------------------------------------------------------------
# Core routing logic
# ---------------------------------------------------------------------------

def classify_action(message_text, intent):
    """Determine which module/tool to route to.

    Returns a tuple:
        ("module", module_name, tool_name)  — for business tasks
        ("service-desk", priority, reason)  — for service desk escalation
    """
    text_lower = message_text.lower()

    # Complaints always go to service desk with HIGH priority
    if intent == "complaint":
        return ("service-desk", "high", "Customer complaint — requires human attention")

    # Feature requests / config changes go to service desk
    if intent == "feature-request":
        # Detect config-change keywords for category assignment
        return ("service-desk", "normal", "Setup/config change request")

    # Technical and unknown go to service desk
    if intent in ("technical", "unknown"):
        return ("service-desk", "normal", f"Intent classified as {intent}")

    # Business intents — try keyword routing
    if intent in _BUSINESS_INTENTS:
        for keywords, module, tool in _ROUTING_RULES:
            for kw in keywords:
                if kw in text_lower:
                    return ("module", module, tool)

        # Business intent but no keyword match — still route to service desk
        return ("service-desk", "normal", f"Business intent '{intent}' but no module keyword match")

    # Fallback: anything else goes to service desk
    return ("service-desk", "normal", f"Unhandled intent: {intent}")


def _execute_tool(module, tool, message_text, data_dir):
    """Simulate executing a module tool. Returns a response string.

    In production this would call the actual module hook/agent.
    For now, returns a confirmation message acknowledging the request.
    """
    # Ensure data dir exists for future tool state
    os.makedirs(data_dir, exist_ok=True)

    responses = {
        "invoicing": f"I'll prepare that invoice for you. Your request has been queued for processing.",
        "estimator": f"I'll work on that estimate. Your request has been queued for processing.",
        "scheduling": f"I'll check the schedule for you. Your request has been queued for processing.",
        "crm": f"I'll look up that contact information. Your request has been queued for processing.",
        "permit-tracker": f"I'll check on the permit status. Your request has been queued for processing.",
    }
    return responses.get(tool, f"Request routed to {module}/{tool}. Queued for processing.")


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def process_message(message, data_dir=".harness/data"):
    """Process a single inbox message — route to tool or service desk.

    Args:
        message: dict with inbox message fields
        data_dir: path to data directory for tool state

    Returns:
        dict with keys: action, module, tool, response, ticket_id (if service desk)
    """
    text = message.get("text", "")
    intent = message.get("intent", "unknown")

    action = classify_action(text, intent)

    if action[0] == "module":
        _, module, tool = action
        response = _execute_tool(module, tool, text, data_dir)
        return {
            "action": "module",
            "module": module,
            "tool": tool,
            "response": response,
        }

    elif action[0] == "service-desk":
        _, priority, reason = action

        # Determine category from intent
        category_map = {
            "feature-request": "feature-request",
            "complaint": "complaint",
            "technical": "other",
            "unknown": "other",
        }
        category = category_map.get(intent, "other")

        # Create service desk ticket
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from service_desk import create_ticket

        ticket = create_ticket(
            source_message_id=message.get("id", ""),
            client=message.get("metadata", {}).get("client", "unknown"),
            requester=message.get("from", "unknown"),
            channel=message.get("channel", "unknown"),
            request=text,
            priority=priority,
            category=category,
        )

        return {
            "action": "service-desk",
            "ticket_id": ticket["id"],
            "priority": priority,
            "reason": reason,
            "response": f"Your request has been forwarded to our team (ticket {ticket['id'][:8]}). We'll follow up shortly.",
        }

    # Should not reach here, but just in case
    return {
        "action": "error",
        "response": "Unable to process request.",
    }


def process_inbox(inbox_dir=".harness/inbox", data_dir=".harness/data"):
    """Process all pending/auto-dispatched messages in the inbox.

    Returns:
        list of dicts, one per processed message, with keys:
            msg_id, action, module, tool, response, ticket_id
    """
    if not os.path.isdir(inbox_dir):
        return []

    results = []

    for fname in sorted(os.listdir(inbox_dir)):
        if not fname.endswith(".json"):
            continue

        path = os.path.join(inbox_dir, fname)
        try:
            msg = load_json(path)
        except (json.JSONDecodeError, IOError):
            continue

        disposition = msg.get("disposition", "")
        if disposition not in ("pending", "auto-dispatched"):
            continue

        msg_id = msg.get("id", fname.replace(".json", ""))
        result = process_message(msg, data_dir=data_dir)
        result["msg_id"] = msg_id

        # Update the inbox message with response and mark as replied
        msg["disposition"] = "replied"
        msg["response"] = result.get("response", "")
        msg["responded_at"] = _now_iso()
        if result.get("ticket_id"):
            msg["service_desk_ticket"] = result["ticket_id"]
        save_json(path, msg)

        results.append(result)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """CLI: python3 lib/orchestrator.py [--once] [--watch]"""
    import argparse

    parser = argparse.ArgumentParser(
        description="HARNESS Orchestrator — route inbox messages to module tools or service desk"
    )
    parser.add_argument("--once", action="store_true",
                        help="Process all pending messages and exit")
    parser.add_argument("--watch", action="store_true",
                        help="Poll every 10 seconds (max 100 iterations)")
    parser.add_argument("--inbox-dir", default=".harness/inbox",
                        help="Path to inbox directory")
    parser.add_argument("--data-dir", default=".harness/data",
                        help="Path to data directory")

    args = parser.parse_args()

    if not args.once and not args.watch:
        # Default to --once
        args.once = True

    if args.once:
        results = process_inbox(args.inbox_dir, args.data_dir)
        print(f"HARNESS Orchestrator — processed {len(results)} message(s)")
        for r in results:
            action_desc = f"{r.get('module', '')}/{r.get('tool', '')}" if r["action"] == "module" else f"service-desk (ticket {r.get('ticket_id', '?')[:8]})"
            print(f"  {r['msg_id'][:8]}: {r['action']} → {action_desc}")

    elif args.watch:
        max_iterations = 100
        iteration = 0
        print(f"HARNESS Orchestrator — watching {args.inbox_dir} (max {max_iterations} iterations)")
        while iteration < max_iterations:
            results = process_inbox(args.inbox_dir, args.data_dir)
            if results:
                print(f"  [{_now_iso()}] Processed {len(results)} message(s)")
                for r in results:
                    action_desc = f"{r.get('module', '')}/{r.get('tool', '')}" if r["action"] == "module" else f"service-desk"
                    print(f"    {r['msg_id'][:8]}: {r['action']} → {action_desc}")
            iteration += 1
            if iteration < max_iterations:
                time.sleep(10)

        print(f"HARNESS Orchestrator — circuit breaker: stopped after {max_iterations} iterations")


if __name__ == "__main__":
    main()
