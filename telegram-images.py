#!/usr/bin/env python3
"""PostToolUse hook: send every image and video file Claude Code produces on this machine to a Telegram chat.

After each tool call, finds image files created or changed since that root's previous scan in the
session's working directory, /tmp and the Claude scratchpads, and sends each once as a photo, video
or animation (a document when Telegram won't take that form), path as caption. The scan runs detached so it never slows a tool call down.

Each sent message is recorded in msgmap.json (message id -> session, file), so a reply to it on
Telegram reaches the session that sent it (telegram-replies.py).

Credentials: ~/.claude/hooks/telegram.env (chmod 600) with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
Per-machine options in the same file:
  TELEGRAM_SEND_AS_DOCUMENT=1  send every file as a document (full resolution, no recompression)
  TELEGRAM_INCLUDE_AUDIO=1     send audio files too
State and log: ~/.claude/hooks/telegram-images/.
"""
import fcntl, json, os, subprocess, sys, time, urllib.request, uuid

HOME = os.path.expanduser("~")
ENV = os.path.join(HOME, ".claude/hooks/telegram.env")
STATE = os.path.join(HOME, ".claude/hooks/telegram-images")
STAMP = os.path.join(STATE, "last-scan.json")
SENT = os.path.join(STATE, "sent.json")
LOG = os.path.join(STATE, "log.txt")
LOCK = os.path.join(STATE, "lock")
MSGMAP = os.path.join(STATE, "msgmap.json")

IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".tiff", ".bmp",
          ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".flac", ".aac"}
SKIP_DIRS = {"node_modules", ".git", "build", ".gradle", "DerivedData", "Pods", ".build", "venv", ".venv",
             "Intermediates.noindex", "Build", "SourcePackages", "xcode-frameworks", ".idea", "cr-dd"}
MAX_BYTES = 50 * 1024 * 1024


def log(msg):
    with open(LOG, "a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def env():
    out = {}
    for line in open(ENV):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            out[k] = v
    return out


def roots(cwd):
    found = [cwd, "/tmp", "/private/tmp"] if cwd else ["/tmp", "/private/tmp"]
    seen, out = set(), []
    for r in found:
        real = os.path.realpath(r)
        if os.path.isdir(real) and real not in seen and real != HOME:
            seen.add(real)
            out.append(real)
    return out


def scan(root, since, extensions):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith((".app", ".xcarchive", ".framework"))]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in extensions:
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_mtime > since and 0 < st.st_size <= MAX_BYTES:
                    yield path, st.st_mtime


# Telegram's media methods and their limits; anything outside them goes as a document instead.
PHOTO = {".png", ".jpg", ".jpeg", ".webp"}
ANIMATION = {".gif"}
VIDEO = {".mp4", ".mov", ".m4v"}
PHOTO_MAX_BYTES = 10 * 1024 * 1024


def media_method(path, size, as_document):
    ext = os.path.splitext(path)[1].lower()
    if as_document:
        return "sendDocument", "document"
    if ext in PHOTO and size <= PHOTO_MAX_BYTES:
        return "sendPhoto", "photo"
    if ext in ANIMATION:
        return "sendAnimation", "animation"
    if ext in VIDEO:
        return "sendVideo", "video"
    return "sendDocument", "document"


def post(token, chat, path, data, method, field):
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{path[-1000:]}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{os.path.basename(path)}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    if method == "sendVideo":
        parts.insert(2, f'--{boundary}\r\nContent-Disposition: form-data; name="supports_streaming"\r\n\r\ntrue\r\n'.encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.load(resp)
    return body.get("result") if body.get("ok") else None


def send(token, chat, path, as_document=False):
    with open(path, "rb") as fh:
        data = fh.read()
    method, field = media_method(path, len(data), as_document)
    if method != "sendDocument":
        try:
            sent = post(token, chat, path, data, method, field)
            if sent:
                return sent, field
        except Exception as e:
            # Too large, odd dimensions or an unsupported codec: Telegram refuses the media form.
            log(f"{method} refused {path}: {e}; sending as a document")
    return post(token, chat, path, data, "sendDocument", "document"), "document"


def remember(message, kind, path, cwd, session):
    """Which session sent the message, and what — so a reply to it reaches that session."""
    if not (message and session):
        return
    msgmap = json.load(open(MSGMAP)) if os.path.exists(MSGMAP) else {}
    msgmap[str(message["message_id"])] = {"session": session, "path": path, "kind": kind,
                                          "cwd": cwd, "at": time.time()}
    if len(msgmap) > 5000:
        msgmap = dict(list(msgmap.items())[-2000:])
    json.dump(msgmap, open(MSGMAP, "w"))


def worker(cwd, session=""):
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        now = time.time()
        stamps = json.load(open(STAMP)) if os.path.exists(STAMP) else {}
        sent = json.load(open(SENT)) if os.path.exists(SENT) else {}
        config = env()
        token, chat = config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHAT_ID"]
        as_document = config.get("TELEGRAM_SEND_AS_DOCUMENT") == "1"
        extensions = IMAGES | (AUDIO if config.get("TELEGRAM_INCLUDE_AUDIO") == "1" else set())
        for root in roots(cwd):
            if root not in stamps:
                stamps[root] = now
                continue
            for path, mtime in scan(root, stamps[root] - 2, extensions):
                if sent.get(path) == mtime:
                    continue
                try:
                    message, kind = send(token, chat, path, as_document)
                    log(f"{'sent' if message else 'refused'} {path}")
                    remember(message, kind, path, cwd, session)
                except Exception as e:
                    log(f"failed {path}: {e}")
                    continue
                sent[path] = mtime
        if len(sent) > 5000:
            sent = dict(list(sent.items())[-2000:])
            stamps = {r: now for r in stamps}
        json.dump(sent, open(SENT, "w"))
        json.dump(stamps, open(STAMP, "w"))


def main():
    os.makedirs(STATE, exist_ok=True)
    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        worker(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        return
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    cwd, session = event.get("cwd") or "", event.get("session_id") or ""
    subprocess.Popen([sys.executable, __file__, "--worker", cwd, session], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(LOG, "a"))


if __name__ == "__main__":
    main()
