#!/usr/bin/env python3
"""tg_client.py — Telethon user-client backend for the real Telegram UAT harness.

Subcommands:
  login                          interactive; writes API_ID/API_HASH/SESSION_STRING to the env file arg
  send  <bot> <text>             prints {"sent_id":int,"date":iso}
  read  <bot> [--since ID] [--wait S] [--quiet S]   prints JSON array of new incoming messages
  dialogs                        prints JSON array of bot dialogs the account has
  env-upsert <path> KEY=VAL...   idempotent key upsert into an env file (chmod 600); pure/testable

Credentials for send/read/dialogs come from env:
  TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING
Never prints secret values. Telethon is imported lazily so env-upsert needs no deps.
"""
import argparse
import asyncio
import json
import os
import sys
import time


def upsert_env_keys(path, mapping):
    """Insert/replace KEY=value lines in an env file; create if missing; chmod 600.
    Returns the sorted list of keys written."""
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.read().splitlines()
    out, seen = [], set()
    for line in lines:
        parts = line.split("=", 1)
        if len(parts) == 2 and parts[0].strip() in mapping:
            k = parts[0].strip()
            out.append("%s=%s" % (k, mapping[k]))
            seen.add(k)
        else:
            out.append(line)
    for k in mapping:
        if k not in seen:
            out.append("%s=%s" % (k, mapping[k]))
    # Create with 0600 from the start so the secret (e.g. TELEGRAM_SESSION_STRING,
    # which grants full Telegram account access) is never world-readable, not even
    # in the window between create and chmod on a first-time write. O_CREAT honors
    # the mode for new files; the trailing chmod tightens an existing looser file.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    # O_CREAT's mode is honored only when the file is newly created; for a
    # pre-existing file (e.g. left at 0644 by a manual `echo >> operator.env`)
    # the kernel keeps the old perms. fchmod the descriptor to 0600 BEFORE
    # writing any secret bytes so the session string is never world-readable,
    # even briefly. The trailing chmod is belt-and-suspenders.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(path, 0o600)
    return sorted(mapping)


def _cmd_env_upsert(args):
    mapping = {}
    for pair in args.pairs:
        if "=" not in pair:
            sys.stderr.write("env-upsert: expected KEY=VALUE, got %r\n" % pair)
            sys.exit(2)
        k, v = pair.split("=", 1)
        mapping[k.strip()] = v
    upsert_env_keys(args.path, mapping)


def _client():
    """Build an authorized TelegramClient from env creds, or exit 3."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    sess = os.environ.get("TELEGRAM_SESSION_STRING")
    missing = [n for n, v in (
        ("TELEGRAM_API_ID", api_id),
        ("TELEGRAM_API_HASH", api_hash),
        ("TELEGRAM_SESSION_STRING", sess),
    ) if not v]
    if missing:
        sys.stderr.write("tg_client: missing %s; run `harness tenant tg-login` first\n"
                         % ", ".join(missing))
        sys.exit(3)
    return TelegramClient(StringSession(sess), int(api_id), api_hash)


def _cmd_login(args):
    # tg-login is inherently interactive: Telegram sends a one-time code that
    # must be typed in. The in-session `!` prefix has no TTY, so input() would
    # EOF. Detect that and give a clear instruction instead of a traceback.
    if not sys.stdin.isatty():
        sys.stderr.write(
            "tg_client: tg-login needs an interactive terminal (a real TTY) to enter the\n"
            "Telegram login code. Run it in your own Terminal (NOT the in-session '!' prefix):\n"
            "    cd %s && ./bin/harness tenant tg-login\n"
            % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.exit(5)
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    # api_id/api_hash may be pre-set in env (operator.env) to skip these prompts;
    # phone/code/2FA always require the TTY above.
    api_id = os.environ.get("TELEGRAM_API_ID") or input("api_id (from my.telegram.org): ").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH") or input("api_hash: ").strip()

    async def run():
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.start()  # interactively prompts phone, login code, 2FA password
        me = await client.get_me()
        session_string = client.session.save()
        upsert_env_keys(args.env_path, {
            "TELEGRAM_API_ID": api_id,
            "TELEGRAM_API_HASH": api_hash,
            "TELEGRAM_SESSION_STRING": session_string,
        })
        await client.disconnect()
        name = " ".join(filter(None, [me.first_name, me.last_name]))
        print("logged in as %s (id %s); credentials saved to %s"
              % (name or "?", me.id, args.env_path))

    asyncio.run(run())


def _cmd_send(args):
    from telethon.errors import FloodWaitError

    async def run():
        client = _client()
        await client.connect()
        if not await client.is_user_authorized():
            sys.stderr.write("tg_client: session not authorized; re-run tg-login\n")
            sys.exit(3)
        try:
            ent = await client.get_entity(args.bot)
            msg = await client.send_message(ent, args.text)
        except FloodWaitError as e:
            sys.stderr.write("tg_client: Telegram FloodWait %ss; back off and retry\n" % e.seconds)
            sys.exit(4)
        print(json.dumps({"sent_id": msg.id, "date": msg.date.isoformat()}))
        await client.disconnect()

    asyncio.run(run())


def _cmd_read(args):
    from telethon.errors import FloodWaitError

    async def run():
        client = _client()
        await client.connect()
        if not await client.is_user_authorized():
            sys.stderr.write("tg_client: session not authorized; re-run tg-login\n")
            sys.exit(3)
        ent = await client.get_entity(args.bot)
        deadline = time.monotonic() + args.wait
        collected, last_new = {}, None
        while True:
            try:
                msgs = await client.get_messages(ent, min_id=args.since, limit=100)
            except FloodWaitError as e:
                sys.stderr.write("tg_client: Telegram FloodWait %ss\n" % e.seconds)
                sys.exit(4)
            for m in msgs:
                if m.out or m.id <= args.since or m.id in collected:
                    continue
                # Cap reply text: tg-read output flows into a UAT agent's context as
                # untrusted external content. Bound it (mirrors the channel-adapter
                # 2000-char rule in CLAUDE.md) so a runaway/adversarial reply can't
                # blow up the context window. 4000 keeps realistic bot replies intact.
                collected[m.id] = {"id": m.id, "date": m.date.isoformat(),
                                   "text": (m.message or "")[:4000]}
                last_new = time.monotonic()
            now = time.monotonic()
            if collected and last_new is not None and (now - last_new) >= args.quiet:
                break
            if now >= deadline:
                break
            await asyncio.sleep(2.5)
        await client.disconnect()
        print(json.dumps([collected[k] for k in sorted(collected)]))

    asyncio.run(run())


def _cmd_dialogs(args):
    async def run():
        client = _client()
        await client.connect()
        if not await client.is_user_authorized():
            sys.stderr.write("tg_client: session not authorized; re-run tg-login\n")
            sys.exit(3)
        out = []
        async for d in client.iter_dialogs():
            ent = d.entity
            if getattr(ent, "bot", False):
                uname = getattr(ent, "username", None)
                out.append({"username": ("@" + uname) if uname else None,
                            "id": ent.id, "title": d.name})
        await client.disconnect()
        print(json.dumps(out))

    asyncio.run(run())


def main():
    p = argparse.ArgumentParser(prog="tg_client.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("env-upsert")
    sp.add_argument("path")
    sp.add_argument("pairs", nargs="+")
    sp.set_defaults(func=_cmd_env_upsert)

    sp = sub.add_parser("login")
    sp.add_argument("env_path")
    sp.set_defaults(func=_cmd_login)

    sp = sub.add_parser("send")
    sp.add_argument("bot")
    sp.add_argument("text")
    sp.set_defaults(func=_cmd_send)

    sp = sub.add_parser("read")
    sp.add_argument("bot")
    sp.add_argument("--since", type=int, default=0)
    sp.add_argument("--wait", type=int, default=540)
    sp.add_argument("--quiet", type=int, default=3)
    sp.set_defaults(func=_cmd_read)

    sp = sub.add_parser("dialogs")
    sp.set_defaults(func=_cmd_dialogs)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
