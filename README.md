# claude-hooks

User-level Claude Code hooks, shared by every machine:

- `telegram-images.py` (PostToolUse) sends each image or video a session produces to Telegram, and
  records which session sent each message.
- `telegram-replies.py` turns a Telegram **reply** to one of those messages into the next
  instruction for the session that sent it, with the replied-to file as context. While the
  session works the reply arrives after the next tool call (`post`); while it is idle a Stop
  hook with `asyncRewake` wakes it (`stop`).
  The final answer of a turn a reply started goes back to Telegram as a reply to yours; reply
  to that answer to keep going with the same session.

One bot serves every machine: each machine reads the shared queue without confirming fresh
messages and takes only replies to messages it sent. See the docstring in `telegram-replies.py`.

## Install on a machine

```bash
git clone git@github.com:TarasOsiris/claude-hooks.git ~/repo/claude-hooks
# once per machine, never committed:
printf 'TELEGRAM_BOT_TOKEN=...\nTELEGRAM_CHAT_ID=...\n' > ~/.claude/hooks/telegram.env
chmod 600 ~/.claude/hooks/telegram.env
~/repo/claude-hooks/install.sh
```

Update: `git pull && ./install.sh`. Settings and replaced scripts are kept as `.bak`.

State and log: `~/.claude/hooks/telegram-images/`.
