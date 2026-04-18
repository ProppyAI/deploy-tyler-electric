"""iMessage channel adapter — macOS only, uses AppleScript via osascript.

SECURITY NOTES:
- Send uses osascript argv passing (on run argv) — NEVER interpolates text into AppleScript source
- Receive uses parameterized SQLite queries — NEVER uses f-strings in SQL
- Database opened in read-only mode
- Phone numbers validated before use in osascript
"""

import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import uuid

from .base import ChannelAdapter


PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")
# iMessage also supports email-address recipients
EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

APPLESCRIPT_SEND = """
on run argv
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant (item 1 of argv) of targetService
        send (item 2 of argv) to targetBuddy
    end tell
end run
"""


def _get_chat_db_path():
    return os.path.expanduser("~/Library/Messages/chat.db")


def _get_state_path(harness_dir=".harness"):
    path = os.path.join(harness_dir, "adapters")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, "imessage_state.json")


def _load_state(harness_dir=".harness"):
    path = _get_state_path(harness_dir)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"last_rowid": 0}


def _save_state(state, harness_dir=".harness"):
    path = _get_state_path(harness_dir)
    tmp_path = path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp_path, path)


class IMessageAdapter(ChannelAdapter):

    def __init__(self, harness_dir=".harness"):
        self.harness_dir = harness_dir

    def is_available(self) -> bool:
        if platform.system() != "Darwin":
            return False
        db_path = _get_chat_db_path()
        if not os.path.isfile(db_path):
            return False
        # Test actual read access (macOS requires Full Disk Access for chat.db)
        try:
            from urllib.parse import quote
            conn = sqlite3.connect(f"file:{quote(db_path, safe='/')}?mode=ro", uri=True)
            conn.close()
            return True
        except sqlite3.OperationalError:
            return False

    def receive(self) -> list:
        if not self.is_available():
            return []

        db_path = _get_chat_db_path()
        state = _load_state(self.harness_dir)
        last_rowid = state.get("last_rowid", 0)

        messages = []
        conn = None
        try:
            # SECURITY: Read-only mode — prevents accidental writes
            # SECURITY: Quote path for URI safety (handles spaces, #, ? in $HOME)
            from urllib.parse import quote
            conn = sqlite3.connect(f"file:{quote(db_path, safe='/')}?mode=ro", uri=True)
            cursor = conn.cursor()

            # SECURITY: Parameterized query — NEVER interpolate variables into SQL
            cursor.execute(
                """
                SELECT m.ROWID, m.text, m.date, h.id as sender
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.ROWID > ? AND m.is_from_me = 0 AND m.text IS NOT NULL
                ORDER BY m.ROWID ASC
                LIMIT 100
                """,
                (last_rowid,)
            )

            max_rowid = last_rowid
            for row in cursor.fetchall():
                rowid, text, date_val, sender = row
                if not text or not sender:
                    continue

                # SECURITY: Truncate to 2000 chars
                text = text[:2000]

                # iMessage stores dates as CoreData epoch (seconds since 2001-01-01)
                msg_timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if date_val:
                    try:
                        # CoreData epoch offset: 978307200 seconds between Unix and 2001 epochs
                        # date values > 1e17 are nanoseconds (macOS 10.13+)
                        epoch_val = date_val / 1e9 if date_val > 1e17 else date_val
                        unix_ts = epoch_val + 978307200
                        msg_timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(unix_ts))
                    except (ValueError, OSError, OverflowError):
                        pass

                msg = {
                    "id": str(uuid.uuid4()),
                    "channel": "imessage",
                    "from": sender,
                    "to": "",
                    "text": text,
                    "thread_id": None,
                    "timestamp": msg_timestamp,
                    "metadata": {"rowid": rowid}
                }
                messages.append(msg)
                max_rowid = max(max_rowid, rowid)

            if max_rowid > last_rowid:
                state["last_rowid"] = max_rowid
                _save_state(state, self.harness_dir)

        except (sqlite3.Error, OSError) as e:
            # Sanitize: error may contain DB path with OS username
            safe_msg = str(e).replace(os.path.expanduser("~"), "~")
            print(f"  iMessage adapter error: {safe_msg}", file=sys.stderr)
        finally:
            if conn:
                conn.close()

        return messages

    def send(self, recipient: str, text: str) -> bool:
        if not self.is_available():
            return False

        # SECURITY: Validate recipient format before it reaches osascript
        if not PHONE_PATTERN.match(recipient) and not EMAIL_PATTERN.match(recipient):
            print(f"  iMessage: invalid recipient format (must be phone number or email)", file=sys.stderr)
            return False

        # Cap outbound text length (consistent with 2000-char inbound cap)
        text = text[:2000]

        try:
            # SECURITY: osascript argv passing — text is NEVER interpolated into AppleScript source
            result = subprocess.run(
                ["osascript", "-", recipient, text],
                input=APPLESCRIPT_SEND,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0 and result.stderr:
                print(f"  iMessage send failed: {result.stderr.strip()}", file=sys.stderr)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"  iMessage send error: {e}", file=sys.stderr)
            return False
