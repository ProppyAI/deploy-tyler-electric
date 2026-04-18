#!/usr/bin/env python3
"""Inbox queue manager — list, dispatch, reply, failure handling, retention."""

import calendar
import json
import os
import re
import sys
import time
import uuid

_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|[\x00-\x1f\x7f-\x9f]')


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
        # Clean up temp file on any failure (including KeyboardInterrupt)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def mask_phone(phone):
    """Mask phone number for display: +1512555****"""
    if len(phone) > 4:
        return phone[:-4] + "****"
    return "****"


def _sanitize_for_terminal(text):
    """Strip ANSI escape sequences and control characters from text for safe terminal display."""
    return _ANSI_ESCAPE_RE.sub('', text)


def ensure_inbox_dir(inbox_dir):
    """Create inbox directory with restricted permissions."""
    os.makedirs(inbox_dir, exist_ok=True)
    try:
        os.chmod(inbox_dir, 0o700)
    except OSError:
        pass


def queue_message(message, intent, confidence, auto_dispatch, inbox_dir):
    """Write a message to the inbox queue.

    Returns the inbox file path.
    """
    # Validate message ID first, before any side effects
    if not _validate_msg_id(message["id"]):
        raise ValueError(f"Invalid message ID (must be UUID): {message['id']!r}")

    ensure_inbox_dir(inbox_dir)

    inbox_entry = {
        "id": message["id"],
        "channel": message["channel"],
        "from": message["from"],
        "to": message.get("to", ""),
        "text": message["text"],
        "timestamp": message["timestamp"],
        "thread_id": message.get("thread_id"),
        "intent": intent,
        "confidence": confidence,
        "disposition": "auto-dispatched" if auto_dispatch else "pending",
        "session_id": None,
        "response": None,
        "responded_at": None,
        "error": None,
        "metadata": message.get("metadata", {}),
    }

    path = os.path.join(inbox_dir, f"{message['id']}.json")
    save_json(path, inbox_entry)
    return path


def _validate_msg_id(msg_id):
    """Validate msg_id is a UUID to prevent path traversal."""
    try:
        uuid.UUID(msg_id)
        return True
    except (ValueError, AttributeError):
        return False


def update_message(msg_id, inbox_dir, **updates):
    """Update fields on an inbox message."""
    if not _validate_msg_id(msg_id):
        return False
    path = os.path.join(inbox_dir, f"{msg_id}.json")
    if not os.path.isfile(path):
        return False
    try:
        msg = load_json(path)
    except (json.JSONDecodeError, IOError):
        return False
    msg.update(updates)
    save_json(path, msg)
    return True


def get_message(msg_id, inbox_dir):
    """Load a single inbox message by ID."""
    if not _validate_msg_id(msg_id):
        return None
    path = os.path.join(inbox_dir, f"{msg_id}.json")
    if not os.path.isfile(path):
        return None
    return load_json(path)


def list_inbox(inbox_dir, show_all=False, full_phone=False):
    """Print pending messages (or all with show_all)."""
    if not os.path.isdir(inbox_dir):
        print("HARNESS — Inbox\n")
        print("  No messages.")
        return

    messages = []
    for fname in os.listdir(inbox_dir):
        if not fname.endswith(".json"):
            continue
        try:
            msg = load_json(os.path.join(inbox_dir, fname))
            messages.append(msg)
        except (json.JSONDecodeError, IOError):
            continue

    # Sort by timestamp (chronological) instead of random UUID filenames
    messages.sort(key=lambda m: m.get("timestamp", ""), reverse=True)

    if not show_all:
        # Show pending and failed
        messages = [m for m in messages if m.get("disposition") in ("pending", "failed")]

    print("HARNESS — Inbox\n")
    if not messages:
        if show_all:
            print("  No messages.")
        else:
            print("  No pending messages. Use --all to include resolved.")
        return

    # Header
    print(f"  {'ID':<10}{'CHANNEL':<12}{'FROM':<18}{'TEXT':<28}{'INTENT':<14}{'STATUS'}")
    print(f"  {'─' * 94}")

    pending = 0
    failed = 0
    for msg in messages:
        msg_id = msg.get("id", "?")[:8]
        channel = _sanitize_for_terminal(msg.get("channel", "?"))
        sender = _sanitize_for_terminal(msg.get("from", "?"))
        # Only mask phone-number senders (iMessage); Telegram IDs are not PII
        if not full_phone and msg.get("channel") == "imessage":
            sender = mask_phone(sender)
        text = _sanitize_for_terminal(msg.get("text", ""))[:24]
        intent = _sanitize_for_terminal(msg.get("intent", "?"))
        disposition = _sanitize_for_terminal(msg.get("disposition", "?"))

        if disposition == "pending":
            pending += 1
        elif disposition == "failed":
            failed += 1

        print(f"  {msg_id:<10}{channel:<12}{sender:<18}{text:<28}{intent:<14}{disposition}")

    parts = []
    if pending:
        parts.append(f"{pending} pending")
    if failed:
        parts.append(f"{failed} failed")
    total_shown = len(messages)
    print(f"\n  {total_shown} message(s) shown. {', '.join(parts) if parts else ''}")


def _parse_iso_timestamp(ts):
    """Parse ISO 8601 timestamp to epoch seconds. Returns None on failure."""
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _validate_inbox_dir(inbox_dir):
    """SECURITY: Ensure inbox_dir is within this project's .harness/ directory."""
    real_dir = os.path.realpath(inbox_dir)
    allowed_root = os.path.realpath(os.path.join(os.getcwd(), ".harness"))
    if not real_dir.startswith(allowed_root + os.sep) and real_dir != allowed_root:
        safe_path = _sanitize_for_terminal(str(inbox_dir))
        print(f"ERROR: inbox_dir must be within {allowed_root}: {safe_path}", file=sys.stderr)
        sys.exit(2)


def cleanup_old_messages(inbox_dir, max_age_days=90):
    """Delete inbox messages older than max_age_days based on message timestamp."""
    _validate_inbox_dir(inbox_dir)
    if not os.path.isdir(inbox_dir):
        return 0
    cutoff = time.time() - (max_age_days * 86400)
    removed = 0
    for fname in os.listdir(inbox_dir):
        path = os.path.join(inbox_dir, fname)
        if not fname.endswith(".json"):
            continue
        try:
            msg = load_json(path)
            msg_epoch = _parse_iso_timestamp(msg.get("timestamp", ""))
            # Fall back to file mtime if timestamp is missing or unparseable
            if msg_epoch is None:
                msg_epoch = os.stat(path).st_mtime
            if msg_epoch < cutoff:
                os.remove(path)
                removed += 1
        except (json.JSONDecodeError, IOError, OSError):
            continue
    return removed


def main():
    if len(sys.argv) < 2:
        print("Usage:", file=sys.stderr)
        print("  inbox_manager.py list <inbox-dir> [--all] [--full]", file=sys.stderr)
        print("  inbox_manager.py reply <msg-id> <inbox-dir> <harness-root>  (HARNESS_REPLY_TEXT env var)", file=sys.stderr)
        sys.exit(2)

    subcmd = sys.argv[1]

    if subcmd == "list":
        if len(sys.argv) < 3:
            print("Usage: inbox_manager.py list <inbox-dir> [--all] [--full]", file=sys.stderr)
            sys.exit(2)
        inbox_dir = sys.argv[2]
        _validate_inbox_dir(inbox_dir)
        show_all = "--all" in sys.argv
        full_phone = "--full" in sys.argv
        list_inbox(inbox_dir, show_all, full_phone)

    elif subcmd == "reply":
        if len(sys.argv) < 5:
            print("Usage: inbox_manager.py reply <msg-id> <inbox-dir> <harness-root>", file=sys.stderr)
            print("  Response text must be in HARNESS_REPLY_TEXT env var", file=sys.stderr)
            sys.exit(2)
        msg_id = sys.argv[2]
        # SECURITY: Read response text from env var, not CLI arg (avoids ps aux exposure)
        response_text = os.environ.get("HARNESS_REPLY_TEXT", "")
        if not response_text:
            print("ERROR: HARNESS_REPLY_TEXT env var is empty or not set", file=sys.stderr)
            sys.exit(2)
        inbox_dir = sys.argv[3]
        _validate_inbox_dir(inbox_dir)
        harness_root = sys.argv[4]

        msg = get_message(msg_id, inbox_dir)
        if not msg:
            print(f"ERROR: Message '{msg_id}' not found in inbox")
            sys.exit(1)

        # Send reply through channel adapter
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from adapter_registry import send_reply

        config_path = os.path.join(os.getcwd(), "harness.json")
        config = {}
        if os.path.isfile(config_path):
            config = load_json(config_path)

        # Determine recipient: use 'to' for Telegram (chat_id), 'from' for iMessage (phone)
        recipient = msg.get("metadata", {}).get("chat_id", msg.get("from", ""))
        channel = msg.get("channel", "")

        print(f"HARNESS — Replying to {msg_id}\n")
        print(f"  Channel: {_sanitize_for_terminal(channel)}")
        display_recipient = mask_phone(recipient) if channel == "imessage" else _sanitize_for_terminal(recipient)
        print(f"  To: {display_recipient}")

        success = send_reply(channel, recipient, response_text, config)
        if success:
            update_message(msg_id, inbox_dir,
                           disposition="replied",
                           response=response_text,
                           responded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            safe_response = _sanitize_for_terminal(response_text)
            print(f"  Response: \"{safe_response[:60]}...\"" if len(safe_response) > 60 else f"  Response: \"{safe_response}\"")
            print(f"\n  Message sent, marked as replied.")
        else:
            update_message(msg_id, inbox_dir,
                           disposition="failed",
                           error=f"Send failed via {channel}")
            print(f"\n  ERROR: Failed to send via {channel}. Marked as failed.")
            sys.exit(1)

    elif subcmd == "cleanup":
        if len(sys.argv) < 3:
            print("Usage: inbox_manager.py cleanup <inbox-dir> [max-age-days]", file=sys.stderr)
            sys.exit(2)
        inbox_dir = sys.argv[2]
        try:
            max_age = int(sys.argv[3]) if len(sys.argv) > 3 else 90
        except ValueError:
            print(f"ERROR: max-age-days must be an integer, got: {sys.argv[3]}", file=sys.stderr)
            sys.exit(2)
        removed = cleanup_old_messages(inbox_dir, max_age)
        print(f"HARNESS — Inbox cleanup: {removed} message(s) older than {max_age} days removed")

    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
