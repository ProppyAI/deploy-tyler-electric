#!/usr/bin/env python3
"""Format a Supabase Management API SQL response (JSON array of objects)
as CSV on stdout. Uses RFC 4180 quoting via the stdlib csv module.

Input is on stdin; output is on stdout. Errors → stderr + exit 1.
"""

import csv
import json
import sys


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("tenant_format_csv: empty input", file=sys.stderr)
        return 1
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"tenant_format_csv: invalid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(rows, list):
        print(
            f"tenant_format_csv: expected a JSON array, got {type(rows).__name__}",
            file=sys.stderr,
        )
        return 1
    if not rows:
        return 0

    cols: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            print("tenant_format_csv: rows must be objects", file=sys.stderr)
            return 1
        for k in row.keys():
            if k not in cols:
                cols.append(k)

    writer = csv.DictWriter(sys.stdout, fieldnames=cols, quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    for row in rows:
        cleaned = {
            c: "" if row.get(c) is None else row.get(c) for c in cols
        }
        writer.writerow(cleaned)
    return 0


if __name__ == "__main__":
    sys.exit(main())
