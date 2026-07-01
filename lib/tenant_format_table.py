#!/usr/bin/env python3
"""Format a Supabase Management API SQL response (JSON array of objects)
as an ASCII table on stdout. Truncates long cell values to 80 chars.

Input is on stdin; output is on stdout. Errors → stderr + exit 1.
"""

import json
import sys


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("tenant_format_table: empty input", file=sys.stderr)
        return 1
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"tenant_format_table: invalid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(rows, list):
        print(
            f"tenant_format_table: expected a JSON array, got {type(rows).__name__}",
            file=sys.stderr,
        )
        return 1
    if not rows:
        print("(0 rows)")
        return 0

    cols: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            print(
                "tenant_format_table: rows must be objects",
                file=sys.stderr,
            )
            return 1
        for k in row.keys():
            if k not in cols:
                cols.append(k)

    def cell(v: object) -> str:
        if v is None:
            return ""
        s = str(v)
        # Newlines + tabs collapse so the table doesn't fragment
        s = s.replace("\n", " ").replace("\t", " ")
        if len(s) > 80:
            s = s[:77] + "..."
        return s

    widths = {c: len(c) for c in cols}
    formatted_rows: list[dict[str, str]] = []
    for row in rows:
        fr: dict[str, str] = {}
        for c in cols:
            s = cell(row.get(c))
            fr[c] = s
            if len(s) > widths[c]:
                widths[c] = len(s)
        formatted_rows.append(fr)

    def hline() -> str:
        return "+" + "+".join("-" * (widths[c] + 2) for c in cols) + "+"

    def render(values: dict[str, str]) -> str:
        return (
            "|"
            + "|".join(f" {values[c].ljust(widths[c])} " for c in cols)
            + "|"
        )

    print(hline())
    print(render({c: c for c in cols}))
    print(hline())
    for fr in formatted_rows:
        print(render(fr))
    print(hline())
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
