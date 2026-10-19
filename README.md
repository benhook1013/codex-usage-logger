# Codex usage logger

Records Codex allowance, reset times, credits, consumption rates, and errors every 15 minutes using the official [app-server interface](https://learn.chatgpt.com/docs/app-server). Uses your existing login; never starts a model turn.

Requires Linux/WSL with systemd, Python 3.10+, and a logged-in Codex CLI. No Python dependencies.

```sh
python3 install.py
~/.local/bin/codex-usage --status
```

The installer links the service and command directly to this checkout. Keep it on disk. It preserves the current `CODEX_HOME` and `CODEX_SQLITE_HOME` paths; override them with `--codex-home` and `--sqlite-home`. A Windows login shared with WSL should use the same Linux database directory as the working CLI.

History lives at `~/.local/share/codex-usage/usage.csv`. Retention is **90 days, 10,000 rows, or 10 MiB**, whichever removes old readings first. Atomic writes can temporarily use another 10 MiB. No separate debug log or backup collection is kept. The CLI reuses its existing Codex profile/database; its diagnostic logging is disabled for scheduled reads. Codex manages its own profile storage, and systemd manages its shared journal.

The timer resumes when WSL starts. It cannot sample while WSL is stopped or Windows is asleep, and does not launch WSL at Windows startup. For operation without a login session, enable user lingering with `loginctl enable-linger "$USER"` if needed.

Missing fields stay blank. Reset changes suppress misleading rates. Rates use percentage points per hour; rounded upstream percentages limit precision. Credit changes show net balance movement, not spending attribution.

```sh
# Stop future readings, keeping history
env -u DBUS_SESSION_BUS_ADDRESS systemctl --user disable --now codex-usage.timer

# Tests (synthetic responses; no account requests)
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
```
