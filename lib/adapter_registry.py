#!/usr/bin/env python3
"""Adapter registry — poll channels, classify messages, dispatch or queue."""

import calendar
import json
import os
import sys
import time


def load_json(path):
    with open(path) as f:
        return json.load(f)


# Auto-dispatchable intents
AUTO_DISPATCH_INTENTS = {"question", "scheduling", "billing-inquiry", "status-check"}

# Rate limit: max auto-dispatches per sender per window
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 3600  # 60 minutes in seconds

# Follow-up escalation: raised threshold for recent auto-dispatch recipients
FOLLOWUP_WINDOW = 600  # 10 minutes
FOLLOWUP_THRESHOLD = 0.95


def mask_phone(phone):
    """Mask phone number for display: +1512555**** """
    if len(phone) > 4:
        return phone[:-4] + "****"
    return "****"


def get_enabled_adapters(config):
    """Return list of enabled channel names from harness.json channels config."""
    channels = config.get("channels", {})
    return [name for name, cfg in channels.items()
            if isinstance(cfg, dict) and cfg.get("enabled", False) is True and name != "rc"]


def _get_adapter(channel_name, harness_dir=".harness", channel_config=None):
    """Get an adapter instance by channel name."""
    lib_dir = os.path.dirname(os.path.abspath(__file__))
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    if channel_name == "imessage":
        from adapters.imessage import IMessageAdapter
        return IMessageAdapter(harness_dir)
    elif channel_name == "telegram":
        from adapters.telegram import TelegramAdapter
        token_env = (channel_config or {}).get("bot_token_env", "HARNESS_TELEGRAM_TOKEN")
        return TelegramAdapter(harness_dir, token_env=token_env)
    return None


def poll_all(config, harness_dir=".harness"):
    """Poll all enabled adapters, return list of normalized messages."""
    enabled = get_enabled_adapters(config)
    all_messages = []

    channels = config.get("channels", {})
    for channel_name in enabled:
        channel_cfg = channels.get(channel_name, {})
        adapter = _get_adapter(channel_name, harness_dir, channel_config=channel_cfg)
        if adapter is None:
            continue
        if not adapter.is_available():
            print(f"  {channel_name}: not available (skipped)")
            continue

        messages = adapter.receive()
        all_messages.extend(messages)
        print(f"  {channel_name}: {len(messages)} new message(s)")

    return all_messages


def send_reply(channel, recipient, text, config, harness_dir=".harness"):
    """Send a reply through the specified channel adapter."""
    channel_cfg = config.get("channels", {}).get(channel, {})
    adapter = _get_adapter(channel, harness_dir, channel_config=channel_cfg)
    if adapter is None or not adapter.is_available():
        return False
    return adapter.send(recipient, text)


def should_auto_dispatch(intent, confidence, threshold, sender, inbox_dir):
    """Determine if a message should be auto-dispatched.

    Checks:
    1. Intent is in auto-dispatchable set
    2. Confidence meets threshold (raised for recent follow-ups)
    3. Sender has not exceeded rate limit
    """
    if intent not in AUTO_DISPATCH_INTENTS:
        return False

    # Check for recent auto-dispatch to this sender (escalation)
    effective_threshold = threshold
    if _sender_has_recent_auto_dispatch(sender, inbox_dir):
        effective_threshold = FOLLOWUP_THRESHOLD

    if confidence < effective_threshold:
        return False

    # Check rate limit
    if _sender_exceeds_rate_limit(sender, inbox_dir):
        return False

    return True


def _parse_iso_timestamp(ts):
    """Parse ISO 8601 timestamp to epoch seconds. Returns None on failure."""
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _sender_has_recent_auto_dispatch(sender, inbox_dir):
    """Check if sender had a message auto-dispatched within FOLLOWUP_WINDOW."""
    if not os.path.isdir(inbox_dir):
        return False
    now = time.time()
    for fname in os.listdir(inbox_dir):
        if not fname.endswith(".json"):
            continue
        try:
            msg = load_json(os.path.join(inbox_dir, fname))
            if (msg.get("from") == sender and
                msg.get("disposition") == "auto-dispatched"):
                # Use message timestamp (always set at queue time)
                msg_epoch = _parse_iso_timestamp(msg.get("timestamp", ""))
                if msg_epoch and (now - msg_epoch) < FOLLOWUP_WINDOW:
                    return True
        except (json.JSONDecodeError, IOError):
            continue
    return False


def _sender_exceeds_rate_limit(sender, inbox_dir):
    """Check if sender has exceeded RATE_LIMIT_MAX auto-dispatches in RATE_LIMIT_WINDOW."""
    if not os.path.isdir(inbox_dir):
        return False
    now = time.time()
    count = 0
    for fname in os.listdir(inbox_dir):
        if not fname.endswith(".json"):
            continue
        try:
            msg = load_json(os.path.join(inbox_dir, fname))
            if (msg.get("from") == sender and
                msg.get("disposition") == "auto-dispatched"):
                # Use message timestamp (always set at queue time) not responded_at
                msg_epoch = _parse_iso_timestamp(msg.get("timestamp", ""))
                if msg_epoch and (now - msg_epoch) < RATE_LIMIT_WINDOW:
                    count += 1
        except (json.JSONDecodeError, IOError):
            continue
    return count >= RATE_LIMIT_MAX


def list_channels(config, harness_dir=".harness"):
    """Print channel status."""
    channels = config.get("channels", {})
    print("HARNESS — Channels\n")
    enabled_count = 0
    total = 0
    for name, cfg in sorted(channels.items()):
        if not isinstance(cfg, dict):
            continue
        total += 1
        is_enabled = cfg.get("enabled", False) is True
        if is_enabled:
            enabled_count += 1

        # Check adapter availability
        status = "enabled" if is_enabled else "disabled"
        if is_enabled and name != "rc":
            adapter = _get_adapter(name, harness_dir, channel_config=cfg)
            if adapter and not adapter.is_available():
                if name == "telegram":
                    status = "enabled (no token)"
                else:
                    status = "enabled (not available)"

        print(f"  {name:<12} {status}")

    print(f"\n  {enabled_count} of {total} channel(s) enabled")


def main():
    """CLI entry point for adapter registry."""
    if len(sys.argv) < 3:
        print("Usage: adapter_registry.py <subcommand> <harness-root>", file=sys.stderr)
        sys.exit(2)

    subcmd = sys.argv[1]
    harness_root = sys.argv[2]

    # Try to load config from cwd
    config_path = os.path.join(os.getcwd(), "harness.json")
    if not os.path.isfile(config_path):
        print("  NOTE: No harness.json in cwd — using HARNESS template defaults", file=sys.stderr)
        config_path = os.path.join(harness_root, "templates", "repo-bootstrap", "harness.json")
    config = {}
    if os.path.isfile(config_path):
        try:
            config = load_json(config_path)
        except (json.JSONDecodeError, IOError) as e:
            print(f"WARNING: Failed to parse {config_path}: {e}", file=sys.stderr)
            print("  Using default config", file=sys.stderr)

    harness_dir = os.path.join(os.getcwd(), ".harness")

    if subcmd == "list":
        list_channels(config, harness_dir)
    elif subcmd == "poll":
        print("HARNESS — Polling channels...\n")
        messages = poll_all(config, harness_dir)

        # Queue polled messages to inbox for processing
        if messages:
            from inbox_manager import queue_message
            inbox_dir = os.path.join(harness_dir, "inbox")
            queued = 0
            for msg in messages:
                queue_message(msg, intent="unknown", confidence=0.0,
                              auto_dispatch=False, inbox_dir=inbox_dir)
                queued += 1
            print(f"\n  {queued} message(s) queued to inbox (pending classification)")
        else:
            print(f"\n  0 message(s) received")
    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
