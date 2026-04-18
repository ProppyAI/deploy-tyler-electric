#!/usr/bin/env python3
"""
HARNESS Dashboard — generates a single-page HTML business overview.

Reads JSON entity records from .harness/data/, .harness/inbox/, and
.harness/service-desk/, then renders a self-contained HTML file with
inline CSS and JS.  No frameworks, no build step, no external deps.

Usage:
    python3 lib/dashboard.py [--data-dir DIR] [--client NAME] [--output PATH] [--open]
"""

import argparse
import glob
import html
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_all_records(base_dir, entity):
    """Load all JSON records from <base_dir>/<entity>/*.json"""
    records = []
    entity_dir = os.path.join(base_dir, entity)
    if not os.path.isdir(entity_dir):
        return records
    for path in sorted(glob.glob(os.path.join(entity_dir, "*.json"))):
        try:
            with open(path, "r") as f:
                records.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return records


def load_flat_records(directory):
    """Load all JSON files directly inside *directory* (no subdirectories)."""
    records = []
    if not os.path.isdir(directory):
        return records
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        try:
            with open(path, "r") as f:
                records.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return records


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def esc(value):
    """HTML-escape a value, converting None to empty string."""
    if value is None:
        return ""
    return html.escape(str(value))


def fmt_currency(value):
    """Format a number as USD ($1,234.56).  Returns '' for None."""
    if value is None:
        return ""
    try:
        return f"${float(value):,.2f}"
    except (ValueError, TypeError):
        return esc(value)


def fmt_date(value):
    """Format an ISO date or datetime as 'Apr 15, 2026'.  Returns '' for None."""
    if not value:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%b %d, %Y")
        except ValueError:
            continue
    return esc(value)


def status_class(status):
    """Map a status string to a CSS class name for badge colouring."""
    if not status:
        return "badge-neutral"
    s = status.lower()
    if s in ("paid", "approved", "complete"):
        return "badge-green"
    if s in ("pending", "sent", "scheduled", "draft", "in-progress", "submitted"):
        return "badge-yellow"
    if s in ("overdue", "rejected", "expired", "cancelled", "failed"):
        return "badge-red"
    return "badge-neutral"


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------

def _stat_card(label, value):
    return f'<div class="stat-card"><div class="stat-value">{esc(str(value))}</div><div class="stat-label">{esc(label)}</div></div>'


def _no_data():
    return '<p class="no-data">No data yet</p>'


def _build_clients_table(clients, jobs):
    if not clients:
        return _no_data()
    job_counts = {}
    for j in jobs:
        cid = j.get("client_id", "")
        job_counts[cid] = job_counts.get(cid, 0) + 1

    rows = []
    for c in sorted(clients, key=lambda r: r.get("created_at", ""), reverse=True):
        phone = esc(c.get("phone", ""))
        email = esc(c.get("email", ""))
        n_jobs = job_counts.get(c.get("client_id", ""), 0)
        rows.append(
            f'<tr><td>{esc(c.get("name"))}</td><td>{phone}</td>'
            f'<td>{email}</td><td class="num">{n_jobs}</td></tr>'
        )
    return (
        '<div class="table-wrap"><table>'
        '<thead><tr><th>Name</th><th>Phone</th><th>Email</th><th># Jobs</th></tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody></table></div>'
    )


def _build_estimates_table(estimates, jobs, clients):
    if not estimates:
        return _no_data()
    client_map = {c["client_id"]: c.get("name", "") for c in clients if "client_id" in c}

    rows = []
    for e in sorted(estimates, key=lambda r: r.get("created_at", ""), reverse=True):
        client_name = client_map.get(e.get("client_id", ""), "")
        desc = esc(e.get("notes", ""))
        status = e.get("status", "")
        rows.append(
            f'<tr><td>{esc(client_name)}</td><td>{desc}</td>'
            f'<td class="num">{fmt_currency(e.get("grand_total"))}</td>'
            f'<td><span class="badge {status_class(status)}">{esc(status)}</span></td></tr>'
        )
    return (
        '<div class="table-wrap"><table>'
        '<thead><tr><th>Client</th><th>Description</th><th>Total</th><th>Status</th></tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody></table></div>'
    )


def _build_invoices_table(invoices, jobs, clients):
    if not invoices:
        return _no_data()
    client_map = {c["client_id"]: c.get("name", "") for c in clients if "client_id" in c}

    rows = []
    for inv in sorted(invoices, key=lambda r: r.get("created_at", ""), reverse=True):
        client_name = client_map.get(inv.get("client_id", ""), "")
        status = inv.get("status", "")
        payment = ""
        if inv.get("paid_at"):
            payment = f"Paid {fmt_date(inv['paid_at'])}"
        elif inv.get("due_date"):
            payment = f"Due {fmt_date(inv['due_date'])}"
        rows.append(
            f'<tr><td>{esc(client_name)}</td>'
            f'<td class="num">{fmt_currency(inv.get("total"))}</td>'
            f'<td><span class="badge {status_class(status)}">{esc(status)}</span></td>'
            f'<td>{esc(payment)}</td></tr>'
        )
    return (
        '<div class="table-wrap"><table>'
        '<thead><tr><th>Client</th><th>Amount</th><th>Status</th><th>Payment</th></tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody></table></div>'
    )


def _build_schedule_section(schedule, jobs, clients):
    if not schedule:
        return _no_data()
    client_map = {c["client_id"]: c.get("name", "") for c in clients if "client_id" in c}
    job_map = {}
    for j in jobs:
        if "id" in j:
            job_map[j["id"]] = j

    items = []
    for s in sorted(schedule, key=lambda r: r.get("date", "")):
        job = job_map.get(s.get("job_id", ""), {})
        cid = job.get("client_id", "")
        client_name = client_map.get(cid, "")
        desc = job.get("description", job.get("type", ""))
        crew_count = len(s.get("crew_member_ids", []))
        time_str = s.get("start_time", "")
        duration = s.get("duration_hours", "")
        status = s.get("status", "")
        items.append(
            f'<div class="schedule-item">'
            f'<div class="schedule-date">{esc(fmt_date(s.get("date")))}</div>'
            f'<div class="schedule-detail">'
            f'<strong>{esc(client_name)}</strong> &mdash; {esc(desc)}<br>'
            f'<span class="meta">Crew: {crew_count} &bull; {esc(time_str)} &bull; {esc(str(duration))}h</span>'
            f'</div>'
            f'<span class="badge {status_class(status)}">{esc(status)}</span>'
            f'</div>'
        )
    return "\n".join(items)


def _build_permits_table(permits, jobs, clients):
    if not permits:
        return _no_data()
    client_map = {c["client_id"]: c.get("name", "") for c in clients if "client_id" in c}
    job_map = {}
    for j in jobs:
        if "id" in j:
            job_map[j["id"]] = j

    rows = []
    for p in sorted(permits, key=lambda r: r.get("filed_at", r.get("permit_id", "")), reverse=True):
        job = job_map.get(p.get("job_id", ""), {})
        cid = job.get("client_id", "")
        client_name = client_map.get(cid, "")
        job_desc = job.get("description", job.get("type", ""))
        status = p.get("status", "")
        rows.append(
            f'<tr><td>{esc(client_name)} &mdash; {esc(job_desc)}</td>'
            f'<td>{esc(p.get("jurisdiction", ""))}</td>'
            f'<td>{esc(p.get("permit_type", ""))}</td>'
            f'<td><span class="badge {status_class(status)}">{esc(status)}</span></td></tr>'
        )
    return (
        '<div class="table-wrap"><table>'
        '<thead><tr><th>Job</th><th>Jurisdiction</th><th>Type</th><th>Status</th></tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody></table></div>'
    )


def _build_inbox_section(inbox):
    if not inbox:
        return _no_data()
    rows = []
    for msg in sorted(inbox, key=lambda r: r.get("received_at", r.get("created_at", "")), reverse=True):
        sender = msg.get("sender_name", msg.get("sender", ""))
        text = msg.get("text", msg.get("message", ""))
        intent = msg.get("intent", "")
        disposition = msg.get("disposition", msg.get("status", ""))
        ts = fmt_date(msg.get("received_at", msg.get("created_at", "")))
        rows.append(
            f'<tr><td>{esc(sender)}</td><td class="msg-text">{esc(text)}</td>'
            f'<td><span class="badge badge-neutral">{esc(intent)}</span></td>'
            f'<td>{esc(disposition)}</td><td>{esc(ts)}</td></tr>'
        )
    return (
        '<div class="table-wrap"><table>'
        '<thead><tr><th>From</th><th>Message</th><th>Intent</th><th>Disposition</th><th>Date</th></tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody></table></div>'
    )


def _build_service_desk_section(tickets):
    if not tickets:
        return _no_data()
    rows = []
    for t in sorted(tickets, key=lambda r: r.get("created_at", ""), reverse=True):
        subject = t.get("subject", t.get("title", ""))
        status = t.get("status", "")
        priority = t.get("priority", "")
        ts = fmt_date(t.get("created_at", ""))
        rows.append(
            f'<tr><td>{esc(subject)}</td>'
            f'<td><span class="badge {status_class(status)}">{esc(status)}</span></td>'
            f'<td>{esc(priority)}</td><td>{esc(ts)}</td></tr>'
        )
    return (
        '<div class="table-wrap"><table>'
        '<thead><tr><th>Subject</th><th>Status</th><th>Priority</th><th>Date</th></tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody></table></div>'
    )


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:#f0f2f5;color:#333;line-height:1.5}
header{background:#1a1a2e;color:#fff;padding:24px 32px;display:flex;
  align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
header h1{font-size:1.5rem;font-weight:700}
header .meta{font-size:.85rem;opacity:.75}
.vertical-badge{background:#0066cc;color:#fff;padding:4px 14px;border-radius:12px;
  font-size:.8rem;font-weight:600;letter-spacing:.5px}
.container{max-width:1200px;margin:0 auto;padding:24px 16px}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:32px}
.stat-card{background:#fff;border-radius:10px;padding:20px;text-align:center;
  box-shadow:0 1px 3px rgba(0,0,0,.08)}
.stat-value{font-size:2rem;font-weight:700;color:#1a1a2e}
.stat-label{font-size:.85rem;color:#666;margin-top:4px}
section{background:#fff;border-radius:10px;padding:24px;margin-bottom:24px;
  box-shadow:0 1px 3px rgba(0,0,0,.08)}
section h2{font-size:1.15rem;font-weight:700;color:#1a1a2e;margin-bottom:16px;
  padding-bottom:8px;border-bottom:2px solid #e8eaed}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th{text-align:left;padding:10px 12px;background:#f7f8fa;color:#555;
  font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:.3px}
td{padding:10px 12px;border-top:1px solid #eee}
tr:hover td{background:#f9fafc}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.msg-text{max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{display:inline-block;padding:3px 10px;border-radius:10px;font-size:.75rem;
  font-weight:600;text-transform:capitalize}
.badge-green{background:#e6f9ee;color:#1a8a4a}
.badge-yellow{background:#fff7e0;color:#8a6d1b}
.badge-red{background:#fde8e8;color:#b91c1c}
.badge-neutral{background:#eef0f4;color:#555}
.schedule-item{display:flex;align-items:center;gap:16px;padding:12px 0;
  border-bottom:1px solid #eee}
.schedule-item:last-child{border-bottom:none}
.schedule-date{min-width:110px;font-weight:600;color:#0066cc;font-size:.9rem}
.schedule-detail{flex:1}
.schedule-detail .meta{font-size:.8rem;color:#888}
.no-data{color:#999;font-style:italic;padding:12px 0}
.footer{text-align:center;padding:24px;font-size:.8rem;color:#999}
@media(max-width:600px){
  header{padding:16px}
  header h1{font-size:1.15rem}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  section{padding:16px}
  td,th{padding:8px 6px;font-size:.8rem}
}
"""

def generate_dashboard(data_dir=".harness/data", inbox_dir=".harness/inbox",
                       service_desk_dir=".harness/service-desk",
                       client_name="Client", vertical="",
                       output_path=".harness/dashboard.html"):
    """Generate the HTML dashboard file."""
    # Load records
    clients = load_all_records(data_dir, "clients")
    estimates = load_all_records(data_dir, "estimates")
    invoices = load_all_records(data_dir, "invoices")
    schedule = load_all_records(data_dir, "schedules")
    permits = load_all_records(data_dir, "permits")
    jobs = load_all_records(data_dir, "jobs")
    inbox = load_flat_records(inbox_dir)
    tickets = load_flat_records(service_desk_dir)

    now = datetime.now(timezone.utc).strftime("%b %d, %Y at %I:%M %p UTC")

    # Quick stats
    open_estimates = sum(1 for e in estimates if e.get("status") in ("draft", "sent"))
    pending_invoices = sum(1 for i in invoices if i.get("status") in ("sent", "overdue"))
    upcoming_schedule = sum(1 for s in schedule if s.get("status") in ("scheduled",))
    open_permits = sum(1 for p in permits if p.get("status") in ("pending", "submitted", "filed"))

    stats_html = (
        _stat_card("Clients", len(clients))
        + _stat_card("Open Estimates", open_estimates)
        + _stat_card("Pending Invoices", pending_invoices)
        + _stat_card("Upcoming Schedule", upcoming_schedule)
        + _stat_card("Open Permits", open_permits)
    )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(client_name)} Dashboard</title>
<style>{CSS}</style>
</head>
<body>

<header>
  <div>
    <h1>{esc(client_name)}</h1>
    <div class="meta">Last updated: {esc(now)}</div>
  </div>
  {f'<span class="vertical-badge">{esc(vertical)}</span>' if vertical else ''}
</header>

<div class="container">

  <div class="stats-grid">{stats_html}</div>

  <section id="clients">
    <h2>Clients</h2>
    {_build_clients_table(clients, jobs)}
  </section>

  <section id="estimates">
    <h2>Estimates</h2>
    {_build_estimates_table(estimates, jobs, clients)}
  </section>

  <section id="invoices">
    <h2>Invoices</h2>
    {_build_invoices_table(invoices, jobs, clients)}
  </section>

  <section id="schedule">
    <h2>Schedule</h2>
    {_build_schedule_section(schedule, jobs, clients)}
  </section>

  <section id="permits">
    <h2>Permits</h2>
    {_build_permits_table(permits, jobs, clients)}
  </section>

  <section id="inbox">
    <h2>Inbox</h2>
    {_build_inbox_section(inbox)}
  </section>

  <section id="service-desk">
    <h2>Service Desk</h2>
    {_build_service_desk_section(tickets)}
  </section>

</div>

<div class="footer">Generated by HARNESS &mdash; {esc(now)}</div>

</body>
</html>"""

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(page)

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """CLI: python3 lib/dashboard.py [--data-dir DIR] [--client NAME] [--output PATH] [--open]"""
    parser = argparse.ArgumentParser(description="Generate HARNESS business dashboard")
    parser.add_argument("--data-dir", default=".harness/data",
                        help="Path to the data directory (default: .harness/data)")
    parser.add_argument("--inbox-dir", default=".harness/inbox",
                        help="Path to inbox directory (default: .harness/inbox)")
    parser.add_argument("--service-desk-dir", default=".harness/service-desk",
                        help="Path to service-desk directory (default: .harness/service-desk)")
    parser.add_argument("--client", default="Client",
                        help="Company name shown in the header")
    parser.add_argument("--vertical", default="",
                        help="Vertical label shown in the header badge (e.g. Electrical)")
    parser.add_argument("--output", default=".harness/dashboard.html",
                        help="Output HTML path (default: .harness/dashboard.html)")
    parser.add_argument("--open", action="store_true",
                        help="Open the generated dashboard in the default browser")
    args = parser.parse_args()

    path = generate_dashboard(
        data_dir=args.data_dir,
        inbox_dir=args.inbox_dir,
        service_desk_dir=args.service_desk_dir,
        client_name=args.client,
        vertical=args.vertical,
        output_path=args.output,
    )
    print(f"Dashboard generated: {path}")

    if args.open:
        webbrowser.open(f"file://{os.path.abspath(path)}")


if __name__ == "__main__":
    main()
