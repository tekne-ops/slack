# Slack workspace inventory

Small utilities for **documenting a Slack workspace** over the Web API—useful for migrations, audits, or compliance prep on **paid workspaces such as Slack Pro**. Slack does not offer a single “export everything” API; this project calls the main **list-style** methods, records what succeeded, and stores **`missing_scope`** / **`not_allowed_token_type`** (and similar) errors for anything the token cannot access.

## Contents

| Path | Role |
|------|------|
| `list_slack_resources.py` | Fetches workspace data and writes **JSON** plus optional **CSV** extracts. |
| `requirements.txt` | Python dependency: `slack_sdk` 3.x. |

Generated artifacts (do not commit if they contain PII):

- `slack_inventory.json` — full inventory (by default next to `-o`).
- `users_active.csv`, `users_guests.csv`, `channels_public.csv`, `channels_private.csv`, `channels_archived.csv`, `usergroups.csv`, `slack_connections.csv` — written to the same directory as the JSON unless `--csv-dir` is set (skip with `--no-csv`).

## Requirements

- **Python 3.10+** (tested with 3.14 in development).
- A **Slack app** installed on the target workspace with a **Bot User OAuth Token** (`xoxb-…`) or, for some methods, a **user token** (`xoxp-…`). The script prefers `SLACK_BOT_TOKEN` when both env vars are set.

Install dependencies (recommended: virtualenv):

```bash
cd /path/to/slack
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

If you run `python list_slack_resources.py` with a system interpreter that does not have `slack_sdk`, the script exits with install hints. Prefer:

```bash
.venv/bin/python list_slack_resources.py --help
```

## Slack app: token and scopes

1. Create or open an app at [api.slack.com/apps](https://api.slack.com/apps).
2. Under **OAuth & Permissions**, add **Bot Token Scopes** as needed.
3. **Install to workspace** and copy the **Bot User OAuth Token**.

Set the token (never commit it):

```bash
export SLACK_BOT_TOKEN='xoxb-...'
```

Optional: `export SLACK_USER_TOKEN='xoxp-...'` is only relevant if you switch the script to prioritize user tokens; several admin-style methods still require specific **user** scopes and **admin** roles.

### Scopes that match what the script calls

**Typical bot scopes for broad coverage:**

- `channels:read`, `groups:read`, `im:read`, `mpim:read`
- `users:read`
- `team:read`
- `files:read`
- `usergroups:read`
- `emoji:read`
- `conversations.connect:manage` — Slack Connect–related listing (`conversations.listConnectInvites`, `team.externalTeams.list`)

**Optional:**

- `users:read.email` — populates the **email** column in `users_active.csv` / JSON user summaries (reinstall the app after adding).
- `bookmarks:read`, `pins:read` — used only with `--deep` (pins/bookmarks per public channel).

After changing scopes, **reinstall** the app so the token updates.

### What a bot token cannot do

Some methods return **`not_allowed_token_type`** for `xoxb-…` tokens (for example `reminders.list`, `stars.list`, `team.accessLogs`, `team.integrationLogs`). Those expect a **user** token and often **admin** / legacy **`admin`** scope. **`team.externalTeams.list`** may return **`not_an_enterprise`** on non–Enterprise Grid workspaces—this is a **plan/context** limitation, not something a Pro bot scope fixes.

## Usage

```bash
export SLACK_BOT_TOKEN='xoxb-...'

# Default: slack_inventory.json + CSVs in the current directory
.venv/bin/python list_slack_resources.py --output ./slack_inventory.json

# Optional: more file metadata pages, deep pins/bookmarks scan
.venv/bin/python list_slack_resources.py -o report.json --file-pages 20 --deep --deep-channel-limit 25

# CSVs only in another directory; strip emails everywhere
.venv/bin/python list_slack_resources.py -o ./out/inventory.json --csv-dir ./out/csv --redact

# JSON only
.venv/bin/python list_slack_resources.py -o inventory.json --no-csv
```

### CLI options

| Option | Description |
|--------|-------------|
| `-o`, `--output` | JSON path (default: `slack_inventory.json`). |
| `--token-file` | Read token from a file (use `chmod 600`). |
| `--file-pages` | Max pages for `files.list` (100 files per page). |
| `--deep` | Call `pins.list` and `bookmarks.list` for up to N public channels. |
| `--deep-channel-limit` | Cap for `--deep` (default: 40). |
| `--full-user-profile` | Embed full profile objects in JSON for `users.list` (large / sensitive). |
| `--redact` | Remove email/phone from profiles in JSON and CSVs. |
| `--csv-dir` | Directory for CSV exports (default: parent of `--output`). |
| `--no-csv` | Do not write CSV files. |
| `-v`, `--verbose` | Debug logging. |

## Output

### JSON (`slack_inventory.json`)

- Top-level **`generated_at`**, **`summary`** (`ok` / `failed` counts for resource blocks).
- **`resources`**: one entry per API probe, each with **`ok`**, and either **`data`** or **`error`** (e.g. `missing_scope`, `not_allowed_token_type`, `not_an_enterprise`).
- User objects in JSON are **condensed** unless `--full-user-profile` is set. Custom emoji in JSON keeps a **sample** of names plus counts to limit file size.

### CSV exports

| File | Description |
|------|-------------|
| `users_active.csv` | Non-deleted, non-bot members; **email** needs `users:read.email`. |
| `users_guests.csv` | Guests (`is_restricted` / `is_ultra_restricted`). |
| `channels_public.csv` | Public workspace channels (IMs/MPDMs excluded). |
| `channels_private.csv` | Private channels/groups, not archived. |
| `channels_archived.csv` | Archived workspace channels. |
| `usergroups.csv` | User groups from `usergroups.list`. |
| `slack_connections.csv` | External orgs, pending Connect invites, `is_ext_shared` channels, plus notes when APIs fail. |

Slack Connect listings are **paginated** inside the script where the API supports cursors.

## Security and git

- Treat tokens like passwords; use env vars or a secret manager.
- Inventory and CSVs often contain **PII** (emails, names, channel IDs). Add patterns such as `slack_inventory.json`, `*.csv`, and `.venv/` to **`.gitignore`** if the repository is shared.

## Limitations

- Not a replacement for **Slack Export** (message history), **Enterprise admin** APIs, or **SCIM** provisioning.
- Coverage is **best-effort**: only methods implemented in `list_slack_resources.py` appear in the report.
- Rate limits apply; `--deep` can issue many requests (small delay between channel calls).
