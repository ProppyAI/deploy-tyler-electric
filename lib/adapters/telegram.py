"""Telegram channel adapter — uses Bot API via urllib, no pip dependencies.

SECURITY NOTES:
- Bot token read from os.environ only — NEVER passed as CLI argument
- URLs containing token are NEVER logged — only endpoint path is logged
- Exception messages are sanitized to remove token before logging
- getUpdates response is validated before processing
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .base import ChannelAdapter


def _get_state_path(harness_dir=".harness"):
    path = os.path.join(harness_dir, "adapters")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, "telegram_state.json")


def _load_state(harness_dir=".harness"):
    path = _get_state_path(harness_dir)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"offset": 0}


def _save_state(state, harness_dir=".harness"):
    path = _get_state_path(harness_dir)
    tmp_path = path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp_path, path)


def _sanitize_error(error_msg, token):
    """SECURITY: Remove bot token from error messages before logging."""
    msg = str(error_msg)
    if token:
        msg = msg.replace(token, "[REDACTED]")
        # Also redact URL-encoded form (e.g., : → %3A in bot token)
        try:
            encoded = urllib.parse.quote(token, safe="")
            if encoded != token:
                msg = msg.replace(encoded, "[REDACTED]")
        except Exception:
            pass
    return msg


def _api_call(token, method, params=None):
    """Call Telegram Bot API. Returns parsed JSON or None.

    SECURITY: The full URL (containing token) is NEVER logged.
    """
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if params:
            data = json.dumps(params).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)

        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result

    except Exception as e:
        # SECURITY: Catch broadly — any exception may contain the URL with token
        # (e.g., http.client.RemoteDisconnected, socket.timeout)
        safe_msg = _sanitize_error(str(e), token)
        print(f"  Telegram API error ({method}): {safe_msg}", file=sys.stderr)
        return None


class TelegramAdapter(ChannelAdapter):

    def __init__(self, harness_dir=".harness", token_env="HARNESS_TELEGRAM_TOKEN"):
        self.harness_dir = harness_dir
        self.token_env = token_env

    def _get_token(self):
        return os.environ.get(self.token_env, "")

    def is_available(self) -> bool:
        return bool(self._get_token())

    def receive(self) -> list:
        token = self._get_token()
        if not token:
            return []

        state = _load_state(self.harness_dir)
        offset = state.get("offset", 0)

        params = {"timeout": 0, "limit": 100}
        if offset:
            params["offset"] = offset

        result = _api_call(token, "getUpdates", params)

        # SECURITY: Validate response structure before processing
        if not result or not isinstance(result, dict):
            return []
        if not result.get("ok", False):
            return []
        updates = result.get("result", [])
        if not isinstance(updates, list):
            return []

        messages = []
        max_update_id = offset
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id", 0)
            # Track offset for ALL updates (including non-text) to prevent infinite requeue
            max_update_id = max(max_update_id, update_id)

            msg = update.get("message", {})
            if not isinstance(msg, dict):
                continue

            text = msg.get("text", "")
            if not text:
                continue

            # SECURITY: Truncate to 2000 chars
            text = text[:2000]

            chat = msg.get("chat", {})
            from_user = msg.get("from", {})
            # Prefer immutable numeric id; fall back to sender_chat for channel posts
            sender = str(from_user.get("id", "")) or from_user.get("username", "")
            if not sender:
                sender_chat = msg.get("sender_chat", {})
                sender = str(sender_chat.get("id", "")) or sender_chat.get("username", "")
            chat_id = str(chat.get("id", ""))

            if not sender:
                continue  # Skip messages with no identifiable sender

            # Use Telegram's message.date (Unix timestamp) instead of poll time
            msg_date = msg.get("date")
            if msg_date and isinstance(msg_date, (int, float)):
                msg_timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(msg_date))
            else:
                msg_timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            normalized = {
                "id": str(uuid.uuid4()),
                "channel": "telegram",
                "from": sender,
                "to": chat_id,
                "text": text,
                "thread_id": None,
                "timestamp": msg_timestamp,
                "metadata": {"update_id": update_id, "chat_id": chat_id}
            }
            messages.append(normalized)

        # Save offset (next poll starts after last processed update)
        if max_update_id > offset:
            state["offset"] = max_update_id + 1
            _save_state(state, self.harness_dir)

        return messages

    def send(self, recipient: str, text: str) -> bool:
        token = self._get_token()
        if not token:
            return False

        # SECURITY: Validate chat_id is numeric (optional leading minus for supergroups)
        if not re.fullmatch(r'-?\d+', recipient):
            print(f"  Telegram: invalid chat_id format", file=sys.stderr)
            return False

        # Telegram sendMessage limit is 4096 chars
        result = _api_call(token, "sendMessage", {
            "chat_id": int(recipient),
            "text": text[:4096],
        })

        return result is not None and result.get("ok", False)
