#!/usr/bin/env python3
"""Filter Netlify function log lines to a ±2 minute window around a target
ISO-8601 timestamp. Reads lines on stdin, writes matching lines on stdout.

Lines without a parseable ISO-8601 timestamp at the start (after an optional
``[𝒇 funcname]`` prefix) pass through unchanged so context like the
``Showing logs from functions...`` header is preserved.

Usage:
    netlify logs --since 2h | python3 tenant_logs_filter.py 2026-05-21T22:00:26Z
"""

import re
import sys
from datetime import datetime, timedelta, timezone


ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")


def parse_iso(s: str) -> datetime | None:
    try:
        # datetime.fromisoformat in py3.11+ handles trailing Z; be defensive
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "tenant_logs_filter: pass target timestamp as the single argument",
            file=sys.stderr,
        )
        return 1
    target = parse_iso(sys.argv[1])
    if target is None:
        print(
            f"tenant_logs_filter: could not parse {sys.argv[1]!r} as ISO-8601",
            file=sys.stderr,
        )
        return 1
    window = timedelta(minutes=2)

    for line in sys.stdin:
        m = ISO_RE.search(line)
        if not m:
            sys.stdout.write(line)
            continue
        ts = parse_iso(m.group(1))
        if ts is None:
            sys.stdout.write(line)
            continue
        if abs(ts - target) <= window:
            sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
