#!/usr/bin/env python3
"""Telegram replies as instructions: reply to a message the bot sent, and the Claude Code session
that sent it takes your reply as its next instruction, with what you replied to as context.

telegram-images.py records which session sent each message, and the file it sent (msgmap.json).

ONE bot serves every machine. Telegram keeps a bot's incoming messages in one queue and drops a
message once any reader confirms it (getUpdates with a higher offset), so no machine confirms a
fresh one: each machine reads the queue with a short poll (no long poll — two open long polls
cancel each other with 409), takes only the replies to messages IT sent (its own msgmap), and
remembers which it has handled. Replies for another machine are left for that machine. Messages
older than CONFIRM_AFTER_HOURS are confirmed by whoever sees them, which keeps the queue short;
Telegram drops anything older than 24 hours anyway.

On this machine one process polls at a time (a lock); it files every reply from TELEGRAM_CHAT_ID
to a message this machine sent into the sending session's inbox. A photo or file attached to
the reply is downloaded beside the inbox. The inbox is delivered by:

  post   PostToolUse: mid-turn, as additionalContext next to the tool result. Fast: it reads a
         file and detaches a short poll.
  stop   Stop hook with asyncRewake: after a turn, waits in the background and wakes the idle
         session (exit code 2, the reply on stderr). One waiter per session; it gives up after
         WAIT_HOURS, or once Claude Code has exited.
  poll   one short poll (what `post` detaches).

When a turn was started by a reply, its final answer (the Stop hook's last_assistant_message)
goes back to Telegram as a reply to yours, and is recorded like any sent file, so replying to the
answer keeps the conversation going with the same session.

Anything that is not a reply from the configured chat to a message this machine sent is left
alone. State and log: ~/.claude/hooks/telegram-images/.
"""
import fcntl, json, os, random, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

HOME = os.path.expanduser("~")
ENV = os.path.join(HOME, ".claude/hooks/telegram.env")
STATE = os.path.join(HOME, ".claude/hooks/telegram-images")
MSGMAP = os.path.join(STATE, "msgmap.json")
OFFSET = os.path.join(STATE, "updates-offset")
INBOX = os.path.join(STATE, "inbox")
ATTACHMENTS = os.path.join(STATE, "attachments")
LOG = os.path.join(STATE, "log.txt")
POLL_LOCK = os.path.join(STATE, "poll.lock")
HANDLED = os.path.join(STATE, "handled-updates.json")
ANSWER_LIMIT = 4000  # Telegram's message limit is 4096
WAIT_HOURS = 4
POLL_SECONDS = 5
CONFIRM_AFTER_HOURS = 6


def log(msg):
    with open(LOG, "a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} replies: {msg}\n")


def creds():
    out = {}
    for line in open(ENV):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            out[k] = v
    return out["TELEGRAM_BOT_TOKEN"], out["TELEGRAM_CHAT_ID"]


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def inbox_path(session):
    safe = "".join(c for c in session if c.isalnum() or c in "-_")
    return os.path.join(INBOX, f"{safe}.jsonl")


def attachment_id(msg):
    """The file_id of a photo (largest size) or document attached to the reply, if any."""
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"]
    return (msg.get("document") or {}).get("file_id")


def route(update, chat, msgmap):
    """(session, entry) for a reply to a known bot message from the configured chat, else None.
    Pure, so it can be tested on its own."""
    msg = update.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != str(chat) or (msg.get("from") or {}).get("is_bot"):
        return None
    parent = msg.get("reply_to_message") or {}
    target = msgmap.get(str(parent.get("message_id")))
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not target or not (text or attachment_id(msg)):
        return None
    return target["session"], {
        "text": text,
        "replying_to": {k: target[k] for k in ("path", "kind", "cwd", "excerpt") if target.get(k)},
        "file_id": attachment_id(msg),
        "message_id": msg.get("message_id"),
        "at": msg.get("date"),
    }


def download(token, file_id, message_id):
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}",
                                timeout=30) as resp:
        remote = json.load(resp)["result"]["file_path"]
    os.makedirs(ATTACHMENTS, exist_ok=True)
    local = os.path.join(ATTACHMENTS, f"{message_id}-{os.path.basename(remote)}")
    urllib.request.urlretrieve(f"https://api.telegram.org/file/bot{token}/{remote}", local)
    return local


def poll():
    """One short read of the shared queue, unless another process here holds the poller.
    Returns whether it polled."""
    os.makedirs(INBOX, exist_ok=True)
    with open(POLL_LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        token, chat = creds()
        # Confirms only what is old enough that every machine has had its chance at it.
        offset = int(load(OFFSET, 0) or 0)
        query = urllib.parse.urlencode({"offset": offset, "timeout": 0,
                                        "allowed_updates": json.dumps(["message"])})
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates?{query}",
                                        timeout=20) as resp:
                updates = json.load(resp).get("result", [])
        except urllib.error.HTTPError as e:
            # 409: another machine's read overlapped this one. Try again a moment later.
            if e.code != 409:
                log(f"getUpdates failed: {e}")
            time.sleep(random.uniform(1, 3))
            return True
        except Exception as e:
            log(f"getUpdates failed: {e}")
            time.sleep(5)
            return True
        now = time.time()
        handled = load(HANDLED, {})
        msgmap = load(MSGMAP, {})
        for update in updates:
            uid = str(update["update_id"])
            date = (update.get("message") or {}).get("date") or now
            if now - date > CONFIRM_AFTER_HOURS * 3600:
                offset = max(offset, update["update_id"] + 1)
            if uid in handled:
                continue
            routed = route(update, chat, msgmap)
            if routed is None:
                continue  # another machine's reply, or not a reply at all
            handled[uid] = now
            session, entry = routed
            if entry["file_id"]:
                try:
                    entry["attachment"] = download(token, entry["file_id"], entry["message_id"])
                except Exception as e:
                    log(f"attachment download failed for reply {entry['message_id']}: {e}")
            with open(inbox_path(session), "a") as fh:
                fh.write(json.dumps(entry) + "\n")
            log(f"queued reply {entry['message_id']} for session {session}")
        handled = {k: v for k, v in handled.items() if now - v < 48 * 3600}
        with open(HANDLED, "w") as fh:
            json.dump(handled, fh)
        with open(OFFSET, "w") as fh:
            json.dump(offset, fh)
        return True


def take(session):
    """Every reply waiting for the session, removed from its inbox."""
    path = inbox_path(session)
    claimed = f"{path}.{os.getpid()}"
    try:
        os.rename(path, claimed)
    except OSError:
        return []
    with open(claimed) as fh:
        entries = [json.loads(line) for line in fh if line.strip()]
    os.remove(claimed)
    return entries


def render(entries):
    lines = ["The user replied on Telegram to a message this session sent. Treat each reply as "
             "their next instruction here, in the context of what they replied to:"]
    for e in entries:
        lines.append(f"- Reply: {e['text'] or '(no text)'}")
        about = e.get("replying_to") or {}
        if about.get("path"):
            lines.append(f"  Replying to the {about.get('kind', 'file')} {about['path']}"
                         " (open it to see what they mean).")
        elif about.get("excerpt"):
            lines.append(f"  Replying to this session's earlier answer: {about['excerpt']}")
        if e.get("attachment"):
            lines.append(f"  They attached: {e['attachment']}")
    return "\n".join(lines)


def answer_path(session):
    return inbox_path(session).replace(".jsonl", ".answer.json")


def deliver(session, entries):
    """The replies as Claude's context, remembering which Telegram messages the turn answers."""
    pending = load(answer_path(session), []) + [e["message_id"] for e in entries if e.get("message_id")]
    with open(answer_path(session), "w") as fh:
        json.dump(pending, fh)
    return render(entries)


def remember(message_id, entry):
    with open(MSGMAP + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        msgmap = load(MSGMAP, {})
        msgmap[str(message_id)] = entry
        if len(msgmap) > 5000:
            msgmap = dict(list(msgmap.items())[-2000:])
        with open(MSGMAP, "w") as fh:
            json.dump(msgmap, fh)


def answer(session, text):
    """Sends the turn's final message back as a reply to the Telegram message that started it."""
    pending = load(answer_path(session), [])
    if not pending:
        return
    os.remove(answer_path(session))
    text = (text or "").strip()
    if not text:
        return
    if len(text) > ANSWER_LIMIT:
        text = text[:ANSWER_LIMIT - 1] + "…"
    token, chat = creds()
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": text,
        "reply_parameters": json.dumps({"message_id": pending[-1], "allow_sending_without_reply": True}),
    }).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                    timeout=30) as resp:
            sent = json.load(resp).get("result") or {}
    except Exception as e:
        log(f"answer to {pending[-1]} failed: {e}")
        return
    excerpt = text if len(text) <= 300 else text[:299] + "…"
    remember(sent.get("message_id"), {"session": session, "kind": "answer", "excerpt": excerpt,
                                      "at": time.time()})
    log(f"answered reply {pending[-1]} for session {session}")


def stop(session, last_message):
    answer(session, last_message)
    # One waiter per session: a later turn's Stop finds the earlier waiter still listening.
    os.makedirs(INBOX, exist_ok=True)
    lock = open(os.path.join(INBOX, f".{os.path.basename(inbox_path(session))}.waiter"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    deadline = time.time() + WAIT_HOURS * 3600
    while time.time() < deadline:
        if os.getppid() == 1:  # Claude Code has exited; nobody to wake.
            return 0
        entries = take(session)
        if entries:
            sys.stderr.write(deliver(session, entries) + "\n")
            return 2
        poll()
        time.sleep(POLL_SECONDS)
    return 0


def post(session):
    subprocess.Popen([sys.executable, __file__, "poll"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(LOG, "a"))
    entries = take(session)
    if entries:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": deliver(session, entries)}}))
    return 0


def main():
    os.makedirs(STATE, exist_ok=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "poll":
        poll()
        return 0
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    session = event.get("session_id") or ""
    if not session:
        return 0
    if mode == "stop":
        return stop(session, event.get("last_assistant_message"))
    if mode == "post":
        return post(session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
