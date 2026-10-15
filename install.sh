#!/bin/bash
# Installs Telegram replies-as-instructions for Claude Code on this machine, for every repo
# (user-level ~/.claude/settings.json). Safe to re-run: it replaces the two scripts and adds
# each hook entry only if it isn't there yet. The old settings and scripts are kept as .bak.
#
# Needs ~/.claude/hooks/telegram.env with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID — the same
# bot on every machine.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
hooks="$HOME/.claude/hooks"
settings="$HOME/.claude/settings.json"

[ -f "$hooks/telegram.env" ] || { echo "missing $hooks/telegram.env" >&2; exit 1; }
mkdir -p "$hooks"
for f in telegram-images.py telegram-replies.py; do
  [ -f "$hooks/$f" ] && cp "$hooks/$f" "$hooks/$f.bak"
  cp "$here/$f" "$hooks/$f"
done
[ -f "$settings" ] && cp "$settings" "$settings.bak"

python3 - "$settings" <<'EOF'
import json, os, sys
path = sys.argv[1]
settings = json.load(open(path)) if os.path.exists(path) else {}
hooks = settings.setdefault("hooks", {})
wanted = {
    "PostToolUse": [
        {"type": "command", "command": "python3 ~/.claude/hooks/telegram-images.py"},
        {"type": "command", "command": "python3 ~/.claude/hooks/telegram-replies.py post"},
    ],
    "Stop": [
        {"type": "command", "command": "python3 ~/.claude/hooks/telegram-replies.py stop",
         "asyncRewake": True},
    ],
}
for event, entries in wanted.items():
    groups = hooks.setdefault(event, [])
    present = {h.get("command") for g in groups for h in g.get("hooks", [])}
    missing = [h for h in entries if h["command"] not in present]
    if missing:
        groups.append({"matcher": "*", "hooks": missing})
json.dump(settings, open(path, "w"), indent=2)
print("hooks installed in", path)
EOF
