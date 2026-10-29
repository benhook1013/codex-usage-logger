# Codex usage logger

Records Codex allowance, reset times, credits, consumption rates, and errors every 15 minutes using the official [app-server interface](https://learn.chatgpt.com/docs/app-server). Uses your existing login; never starts a model turn.

Requires Linux/WSL with systemd, Python 3.10+, and a logged-in Codex CLI. No Python dependencies.

```sh
python3 install.py
~/.local/bin/codex-usage --status
~/.local/bin/codex-usage list --day
~/.local/bin/codex-usage list --hour --days 3
```

`list` reads saved history only, defaulting to hourly rows over seven local calendar days. Daily/hourly rows show the last successful observation and its time, remaining percentages, credit balance, reading/error counts, and reset/gap events. Missing data is `-`; these are snapshots, not consumption totals. Use `--days 1` for today or up to `--days 90` for retained history.

The installer links the service and command directly to this checkout. Keep it on disk. It preserves the current `CODEX_HOME` and `CODEX_SQLITE_HOME` paths; override them with `--codex-home` and `--sqlite-home`. A Windows login shared with WSL should use the same Linux database directory as the working CLI.

If the first reading fails, installation stays enabled and shows status/login guidance; later scheduled readings can recover after the underlying problem is fixed.

History lives at `~/.local/share/codex-usage/usage.csv`. Retention is **90 days, 10,000 rows, or 10 MiB**, whichever removes old readings first. Atomic writes can temporarily use another 10 MiB. No separate debug log or backup collection is kept. The CLI reuses its existing Codex profile/database; its diagnostic logging is disabled for scheduled reads. Codex manages its own profile storage, and systemd manages its shared journal.

The timer resumes when WSL starts. It cannot sample while WSL is stopped or Windows is asleep, and does not launch WSL at Windows startup. For operation without a login session, enable user lingering with `loginctl enable-linger "$USER"` if needed.

Missing fields stay blank. Reset changes suppress misleading rates. Rates use percentage points per hour; rounded upstream percentages limit precision. Credit changes show net balance movement, not spending attribution.

```sh
# Stop future readings, keeping history
env -u DBUS_SESSION_BUS_ADDRESS systemctl --user disable --now codex-usage.timer

# Tests (synthetic responses; no account requests)
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
```

CI actions are pinned to exact commits. Renovate opens update PRs, groups routine action updates, and leaves merging to a human. Activate it by granting the [Renovate GitHub app](https://github.com/apps/renovate) access to this repository and merging `renovate.json` into `main`.
