#!/usr/bin/env python3
"""
Inventory Slack workspace resources via the Web API (typical paid plans incl. Pro).

Slack does not expose one "export everything" call. This script issues the main
*list-style* methods that apply to a normal workspace, records successes, and
captures ``missing_scope`` / other errors for anything the token cannot access.

Token
-----
Use a **Bot User OAuth Token** (``xoxb-...``) or **User OAuth Token** (``xoxp-...``).
Set ``SLACK_BOT_TOKEN`` or ``SLACK_USER_TOKEN`` (bot is preferred if both are set).

Suggested Bot Token Scopes (add under *OAuth & Permissions*) for broad coverage::

    channels:read, groups:read, im:read, mpim:read
    users:read
    team:read
    files:read
    usergroups:read
    emoji:read
    conversations.connect:manage

Optional (more data, more sensitivity)::

    users:read.email
    bookmarks:read
    pins:read   # if available for your app type

Some methods (e.g. certain team logs, billing) only succeed for specific roles or
plans; failures are listed in the output under each resource.

Security
--------
Output can include **PII** (emails, names, file metadata, DM channel ids). Do not
commit reports to git. Use ``--redact`` to drop obvious email/phone fields from
user profiles. Prefer a token with the **minimum** scopes you need.

CSV exports (same directory as ``--output`` unless ``--csv-dir`` is set)::

    users_active.csv          Active human members (needs ``users:read.email`` for email column)
    users_guests.csv          Multi- / single-channel guests
    channels_public.csv       Public workspace channels (no DMs)
    channels_private.csv      Private channels / groups (no DMs)
    channels_archived.csv     Archived workspace channels
    usergroups.csv            User groups (IDP groups)
    slack_connections.csv     Slack Connect orgs, pending invites, shared channels

Use ``--no-csv`` to skip CSV files. ``--redact`` clears emails in CSVs and JSON.

Usage::

    export SLACK_BOT_TOKEN='xoxb-...'
    python list_slack_resources.py --output slack_inventory.json
    python list_slack_resources.py --deep --deep-channel-limit 30 --output report.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError:
    sys.stderr.write(
        "Error: slack_sdk is not installed for this Python interpreter.\n\n"
        "Install dependencies (pick one):\n"
        "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
        "  .venv/bin/python list_slack_resources.py --output slack_inventory.json\n\n"
        "Or, if you use another venv or pip:\n"
        "  python3 -m pip install -r requirements.txt\n"
    )
    raise SystemExit(2) from None

_CONVERSATION_TYPES = (
    ("conversations.list [public_channel]", "public_channel"),
    ("conversations.list [private_channel]", "private_channel"),
    ("conversations.list [mpim]", "mpim"),
    ("conversations.list [im]", "im"),
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )


def _get_token(args: argparse.Namespace) -> str:
    if args.token_file:
        p = Path(args.token_file)
        if p.stat().st_mode & 0o077:
            logging.warning(
                "Token file is group/other readable; chmod 600 recommended: %s",
                p,
            )
        return p.read_text(encoding="utf-8").strip()
    token = (os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_USER_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "Set SLACK_BOT_TOKEN or SLACK_USER_TOKEN, or pass --token-file."
        )
    return token


def _slack_response_to_dict(resp: Any) -> dict[str, Any]:
    if hasattr(resp, "data") and isinstance(resp.data, dict):
        return dict(resp.data)
    if isinstance(resp, dict):
        return dict(resp)
    return {"_raw": str(resp)}


def _try_call(
    label: str,
    fn: Callable[[], Any],
) -> dict[str, Any]:
    try:
        resp = fn()
        data = _slack_response_to_dict(resp)
        if data.get("ok") is False:
            return {"ok": False, "error": data.get("error", "unknown"), "label": label}
        return {"ok": True, "label": label, "data": data}
    except SlackApiError as e:
        err = e.response.get("error") if e.response is not None else None
        return {"ok": False, "label": label, "error": err or str(e)}
    except Exception as e:
        return {"ok": False, "label": label, "error": f"exception:{type(e).__name__}:{e}"}


def _redact_user_profile(profile: dict[str, Any]) -> dict[str, Any]:
    out = dict(profile)
    for key in ("email", "phone", "huddle_state_expiration"):
        out.pop(key, None)
    return out


def _condense_user(
    m: dict[str, Any],
    *,
    redact: bool,
    full_profile: bool,
) -> dict[str, Any]:
    profile = m.get("profile") or {}
    if redact and isinstance(profile, dict):
        profile = _redact_user_profile(profile)
    base = {
        "id": m.get("id"),
        "team_id": m.get("team_id"),
        "name": m.get("name"),
        "real_name": profile.get("real_name") if isinstance(profile, dict) else None,
        "is_bot": m.get("is_bot"),
        "is_app_user": m.get("is_app_user"),
        "deleted": m.get("deleted"),
        "is_invited_user": m.get("is_invited_user"),
    }
    if full_profile and isinstance(profile, dict):
        base["profile"] = profile
    return base


def _condense_channel(ch: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ch.get("id"),
        "name": ch.get("name"),
        "is_channel": ch.get("is_channel"),
        "is_group": ch.get("is_group"),
        "is_im": ch.get("is_im"),
        "is_mpim": ch.get("is_mpim"),
        "is_private": ch.get("is_private"),
        "is_archived": ch.get("is_archived"),
        "is_general": ch.get("is_general"),
        "num_members": ch.get("num_members"),
        "topic": (ch.get("topic") or {}).get("value"),
        "purpose": (ch.get("purpose") or {}).get("value"),
        "created": ch.get("created"),
    }


def _topic_purpose_value(field: Any) -> str:
    if isinstance(field, dict):
        return (field.get("value") or "") or ""
    if field is None:
        return ""
    return str(field)


def _is_workspace_channel(ch: dict[str, Any]) -> bool:
    return not ch.get("is_im") and not ch.get("is_mpim")


def _merge_channels_by_id(raw_channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for ch in raw_channels:
        cid = ch.get("id")
        if not cid:
            continue
        merged[cid] = ch
    return list(merged.values())


def _paginate_external_teams(client: WebClient) -> dict[str, Any]:
    orgs: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(100):
        resp = client.team_externalTeams_list(limit=100, cursor=cursor)
        data = _slack_response_to_dict(resp)
        if data.get("ok") is False:
            return data
        orgs.extend(data.get("organizations") or data.get("teams") or [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return {"ok": True, "organizations": orgs, "total_count": len(orgs)}


def _paginate_connect_invites(client: WebClient) -> dict[str, Any]:
    invites: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(100):
        resp = client.conversations_listConnectInvites(limit=200, cursor=cursor)
        data = _slack_response_to_dict(resp)
        if data.get("ok") is False:
            return data
        invites.extend(data.get("invites") or [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return {"ok": True, "invites": invites}


def _user_csv_row(member: dict[str, Any], *, redact: bool) -> dict[str, Any]:
    p = member.get("profile") or {}
    email = (p.get("email") or "") if isinstance(p, dict) else ""
    if redact:
        email = ""
    return {
        "id": member.get("id"),
        "team_id": member.get("team_id"),
        "name": member.get("name"),
        "real_name": p.get("real_name") if isinstance(p, dict) else None,
        "display_name": p.get("display_name") if isinstance(p, dict) else None,
        "email": email or "",
        "is_admin": member.get("is_admin"),
        "is_owner": member.get("is_owner"),
        "is_primary_owner": member.get("is_primary_owner"),
        "is_restricted": member.get("is_restricted"),
        "is_ultra_restricted": member.get("is_ultra_restricted"),
        "is_bot": member.get("is_bot"),
        "is_app_user": member.get("is_app_user"),
        "deleted": member.get("deleted"),
        "timezone": p.get("tz") if isinstance(p, dict) else None,
        "title": p.get("title") if isinstance(p, dict) else None,
    }


def _channel_csv_row(ch: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ch.get("id"),
        "name": ch.get("name") or "",
        "is_private": ch.get("is_private"),
        "is_archived": ch.get("is_archived"),
        "is_general": ch.get("is_general"),
        "is_channel": ch.get("is_channel"),
        "is_group": ch.get("is_group"),
        "num_members": ch.get("num_members"),
        "topic": _topic_purpose_value(ch.get("topic")),
        "purpose": _topic_purpose_value(ch.get("purpose")),
        "created": ch.get("created"),
        "is_ext_shared": ch.get("is_ext_shared"),
        "context_team_id": ch.get("context_team_id"),
    }


USER_CSV_FIELDS = [
    "id",
    "team_id",
    "name",
    "real_name",
    "display_name",
    "email",
    "is_admin",
    "is_owner",
    "is_primary_owner",
    "is_restricted",
    "is_ultra_restricted",
    "is_bot",
    "is_app_user",
    "deleted",
    "timezone",
    "title",
]

CHANNEL_CSV_FIELDS = [
    "id",
    "name",
    "is_private",
    "is_archived",
    "is_general",
    "is_channel",
    "is_group",
    "num_members",
    "topic",
    "purpose",
    "created",
    "is_ext_shared",
    "context_team_id",
]


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: "" if r.get(k) is None else r.get(k) for k in fieldnames})


def _flatten_for_csv_row(prefix: str, obj: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"connection_type": prefix}
    extra: dict[str, Any] = {}
    for k, v in obj.items():
        if isinstance(v, (dict, list)):
            extra[k] = v
        else:
            row[k] = v
    if extra:
        row["extra_json"] = json.dumps(extra, ensure_ascii=False)
    return row


def write_csv_exports(
    directory: Path,
    *,
    members_raw: list[dict[str, Any]],
    channels_merged: list[dict[str, Any]],
    resources: dict[str, Any],
    redact: bool,
) -> None:
    """Write derived CSV tables next to the JSON inventory."""
    # --- Users ---
    active_rows: list[dict[str, Any]] = []
    guest_rows: list[dict[str, Any]] = []
    for m in members_raw:
        if m.get("deleted"):
            continue
        if m.get("is_bot"):
            continue
        row = _user_csv_row(m, redact=redact)
        active_rows.append(row)
        if m.get("is_restricted") or m.get("is_ultra_restricted"):
            guest_rows.append(row)
    _write_csv(directory / "users_active.csv", active_rows, USER_CSV_FIELDS)
    _write_csv(directory / "users_guests.csv", guest_rows, USER_CSV_FIELDS)
    if active_rows and not any(r.get("email") for r in active_rows) and not redact:
        logging.warning(
            "email column is empty for all users; add Bot scope users:read.email and reinstall the app."
        )

    # --- Channels (workspace channels only; exclude IM / MPDM) ---
    wc = [c for c in channels_merged if _is_workspace_channel(c)]
    public_ch = [
        c
        for c in wc
        if not c.get("is_private") and not c.get("is_archived")
        and (c.get("is_channel") or c.get("is_group"))
    ]
    private_ch = [
        c
        for c in wc
        if c.get("is_private") and not c.get("is_archived")
    ]
    archived_ch = [c for c in wc if c.get("is_archived")]

    _write_csv(
        directory / "channels_public.csv",
        [_channel_csv_row(c) for c in public_ch],
        CHANNEL_CSV_FIELDS,
    )
    _write_csv(
        directory / "channels_private.csv",
        [_channel_csv_row(c) for c in private_ch],
        CHANNEL_CSV_FIELDS,
    )
    _write_csv(
        directory / "channels_archived.csv",
        [_channel_csv_row(c) for c in archived_ch],
        CHANNEL_CSV_FIELDS,
    )

    # --- User groups ---
    ug_block = resources.get("usergroups.list") or {}
    ug_rows: list[dict[str, Any]] = []
    if ug_block.get("ok"):
        for ug in ug_block.get("data", {}).get("usergroups") or []:
            users = ug.get("users") or []
            users_str = ",".join(users) if isinstance(users, list) else str(users)
            prefs = ug.get("prefs")
            ug_rows.append(
                {
                    "id": ug.get("id"),
                    "team_id": ug.get("team_id"),
                    "name": ug.get("name"),
                    "handle": ug.get("handle"),
                    "description": ug.get("description"),
                    "user_count": ug.get("user_count"),
                    "users": users_str,
                    "date_create": ug.get("date_create"),
                    "date_update": ug.get("date_update"),
                    "is_external": ug.get("is_external"),
                    "is_usergroup": ug.get("is_usergroup"),
                    "prefs_json": json.dumps(prefs, ensure_ascii=False)
                    if prefs is not None
                    else "",
                }
            )
    ug_fields = [
        "id",
        "team_id",
        "name",
        "handle",
        "description",
        "user_count",
        "users",
        "date_create",
        "date_update",
        "is_external",
        "is_usergroup",
        "prefs_json",
    ]
    _write_csv(directory / "usergroups.csv", ug_rows, ug_fields)

    # --- Slack Connect: external orgs, pending invites, shared channels ---
    conn_rows: list[dict[str, Any]] = []

    ext = resources.get("team.externalTeams.list") or {}
    if ext.get("ok"):
        for org in ext.get("data", {}).get("organizations") or []:
            if not isinstance(org, dict):
                continue
            r = _flatten_for_csv_row("external_organization", org)
            conn_rows.append(r)
    else:
        conn_rows.append(
            {
                "connection_type": "note",
                "detail": "team.externalTeams.list failed",
                "error": ext.get("error", ""),
            }
        )

    inv_block = resources.get("conversations.listConnectInvites") or {}
    if inv_block.get("ok"):
        for inv in inv_block.get("data", {}).get("invites") or []:
            if not isinstance(inv, dict):
                continue
            r = _flatten_for_csv_row("pending_invite", inv)
            conn_rows.append(r)
    else:
        conn_rows.append(
            {
                "connection_type": "note",
                "detail": "conversations.listConnectInvites failed",
                "error": inv_block.get("error", ""),
            }
        )

    for c in wc:
        if c.get("is_ext_shared"):
            ch = _channel_csv_row(c)
            ch["connection_type"] = "shared_channel"
            conn_rows.append(ch)

    sc_keys = sorted({k for row in conn_rows for k in row}) if conn_rows else [
        "connection_type",
        "detail",
        "error",
    ]
    _write_csv(directory / "slack_connections.csv", conn_rows, sc_keys)

    logging.info(
        "Wrote CSV exports under %s (users_active=%d, guests=%d, public_ch=%d, "
        "private_ch=%d, archived_ch=%d, usergroups=%d, connection_rows=%d).",
        directory,
        len(active_rows),
        len(guest_rows),
        len(public_ch),
        len(private_ch),
        len(archived_ch),
        len(ug_rows),
        len(conn_rows),
    )


def collect_inventory(
    client: WebClient,
    *,
    file_page_limit: int,
    redact: bool,
    full_user_profile: bool,
    deep: bool,
    deep_channel_limit: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "resources": {},
    }

    # --- Identity / workspace ---
    auth = _try_call("auth.test", lambda: client.auth_test())
    out["resources"]["auth.test"] = auth
    if auth.get("ok"):
        team_id = auth["data"].get("team_id")
    else:
        team_id = None

    for label, method in (
        ("team.info", lambda: client.team_info()),
        ("team.profile.get", lambda: client.team_profile_get()),
        ("team.preferences.list", lambda: client.team_preferences_list()),
        ("team.externalTeams.list", lambda: _paginate_external_teams(client)),
        ("team.billing.info", lambda: client.team_billing_info()),
        ("team.integrationLogs", lambda: client.team_integrationLogs(count=50, page=1)),
        ("team.accessLogs", lambda: client.team_accessLogs(limit=20)),
    ):
        out["resources"][label] = _try_call(label, method)

    # --- Members ---
    members_raw: list[dict[str, Any]] = []

    def _users():
        members: list[dict[str, Any]] = []
        for page in client.users_list(limit=200):
            members.extend(page.get("members") or [])
        return {"ok": True, "members": members}

    users_result = _try_call("users.list", _users)
    out["resources"]["users.list"] = users_result
    if users_result.get("ok"):
        members_raw = list(users_result["data"].get("members") or [])
        users_result["data"]["member_count"] = len(members_raw)
        users_result["data"]["members"] = [
            _condense_user(m, redact=redact, full_profile=full_user_profile)
            for m in members_raw
        ]

    # --- Conversations (by type so one missing scope does not block others) ---
    all_channels: list[dict[str, Any]] = []
    out["resources"]["conversations.listConnectInvites"] = _try_call(
        "conversations.listConnectInvites",
        lambda: _paginate_connect_invites(client),
    )

    for label, types in _CONVERSATION_TYPES:
        key = f"conversations.list ({types})"

        def _conv(t: str = types) -> dict[str, Any]:
            acc: list[dict[str, Any]] = []
            for page in client.conversations_list(
                types=t,
                limit=200,
                exclude_archived=False,
            ):
                acc.extend(page.get("channels") or [])
            return {"ok": True, "channels": acc}

        r = _try_call(key, _conv)
        out["resources"][key] = r
        if r.get("ok"):
            chans = r["data"].get("channels") or []
            r["data"]["channel_count"] = len(chans)
            r["data"]["channels"] = [_condense_channel(c) for c in chans]
            all_channels.extend(chans)

    # --- User groups ---
    ug = _try_call(
        "usergroups.list",
        lambda: client.usergroups_list(include_users=True),
    )
    out["resources"]["usergroups.list"] = ug

    # --- Emoji ---
    em = _try_call("emoji.list", lambda: client.emoji_list())
    out["resources"]["emoji.list"] = em
    if em.get("ok") and isinstance(em.get("data"), dict):
        emoji_map = em["data"].get("emoji") or {}
        em["data"]["custom_emoji_count"] = len(emoji_map)
        # Full alias→url map can be huge; keep a sample of names only.
        em["data"]["emoji_keys_sample"] = sorted(emoji_map.keys())[:120]
        em["data"].pop("emoji", None)

    # --- Files (capped) ---
    def _files():
        files: list[dict[str, Any]] = []
        cursor = None
        pages_done = 0
        for _ in range(file_page_limit):
            resp = client.files_list(count=100, cursor=cursor)
            data = _slack_response_to_dict(resp)
            pages_done += 1
            if data.get("ok") is False:
                return data
            files.extend(data.get("files") or [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return {"ok": True, "files": files, "pages_fetched": pages_done}

    fr = _try_call("files.list", _files)
    out["resources"]["files.list"] = fr
    if fr.get("ok") and isinstance(fr.get("data"), dict):
        files = fr["data"].get("files") or []
        fr["data"]["file_count_in_report"] = len(files)
        fr["data"]["files"] = [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "title": f.get("title"),
                "mimetype": f.get("mimetype"),
                "filetype": f.get("filetype"),
                "size": f.get("size"),
                "created": f.get("created"),
                "is_public": f.get("is_public"),
                "user": f.get("user"),
                "channels": f.get("channels"),
                "groups": f.get("groups"),
                "ims": f.get("ims"),
            }
            for f in files
        ]

    # --- User-scoped lists (often need xoxp + scopes) ---
    out["resources"]["reminders.list"] = _try_call(
        "reminders.list", lambda: client.reminders_list()
    )
    out["resources"]["stars.list"] = _try_call(
        "stars.list", lambda: client.stars_list(limit=100)
    )

    # --- Per-channel pins / bookmarks (optional; N+1 API calls) ---
    if deep and team_id:
        public_ids = [
            c["id"]
            for c in all_channels
            if c.get("is_channel")
            and not c.get("is_private")
            and not c.get("is_im")
            and not c.get("is_mpim")
        ][:deep_channel_limit]
        pins_reports: list[dict[str, Any]] = []
        bm_reports: list[dict[str, Any]] = []
        for cid in public_ids:
            pr = _try_call(
                f"pins.list [{cid}]",
                lambda c=cid: client.pins_list(channel=c),
            )
            pr["channel_id"] = cid
            pins_reports.append(pr)
            br = _try_call(
                f"bookmarks.list [{cid}]",
                lambda c=cid: client.bookmarks_list(channel_id=c),
            )
            br["channel_id"] = cid
            bm_reports.append(br)
            time.sleep(0.35)
        out["resources"]["pins.list (per public channel)"] = {
            "ok": True,
            "channels_checked": len(public_ids),
            "results": pins_reports,
        }
        out["resources"]["bookmarks.list (per public channel)"] = {
            "ok": True,
            "channels_checked": len(public_ids),
            "results": bm_reports,
        }
    elif deep and not team_id:
        out["resources"]["pins.list (per public channel)"] = {
            "ok": False,
            "error": "auth.test did not return team_id; cannot run --deep",
        }

    channels_merged = _merge_channels_by_id(all_channels)
    out["__export_context"] = {
        "members_raw": members_raw,
        "channels_merged": channels_merged,
    }

    # --- Summary counts ---
    summary: dict[str, Any] = {"ok": 0, "failed": 0}
    for v in out["resources"].values():
        if isinstance(v, dict) and "ok" in v:
            if v.get("ok"):
                summary["ok"] += 1
            else:
                summary["failed"] += 1
        elif isinstance(v, dict) and v.get("results"):
            summary["ok"] += 1
    out["summary"] = summary
    return out


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="List Slack workspace resources via the Web API (Pro and similar).",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("slack_inventory.json"),
        help="Write JSON inventory here (default: slack_inventory.json).",
    )
    p.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="Read token from file (chmod 600). Overrides env vars.",
    )
    p.add_argument(
        "--file-pages",
        type=int,
        default=10,
        metavar="N",
        help="Max pages for files.list (100 files/page). Default: 10.",
    )
    p.add_argument(
        "--deep",
        action="store_true",
        help="Also call pins.list and bookmarks.list for public channels (many API calls).",
    )
    p.add_argument(
        "--deep-channel-limit",
        type=int,
        default=40,
        metavar="N",
        help="With --deep, max public channels to scan. Default: 40.",
    )
    p.add_argument(
        "--full-user-profile",
        action="store_true",
        help="Include full profile dicts in users.list (large; may contain PII).",
    )
    p.add_argument(
        "--redact",
        action="store_true",
        help="Strip email/phone from user profiles in the report.",
    )
    p.add_argument(
        "--csv-dir",
        type=Path,
        default=None,
        help="Write CSV exports here (default: same directory as --output JSON).",
    )
    p.add_argument(
        "--no-csv",
        action="store_true",
        help="Skip users_*.csv, channels_*.csv, usergroups.csv, slack_connections.csv.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _setup_logging(args.verbose)
    try:
        token = _get_token(args)
    except SystemExit as e:
        logging.error("%s", e)
        return 2

    client = WebClient(token=token)
    inv = collect_inventory(
        client,
        file_page_limit=max(1, args.file_pages),
        redact=args.redact,
        full_user_profile=args.full_user_profile,
        deep=args.deep,
        deep_channel_limit=max(1, args.deep_channel_limit),
    )
    export_ctx = inv.pop("__export_context", None) or {}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(inv, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    s = inv.get("summary", {})
    logging.info(
        "Wrote %s (%d resource blocks ok / %d failed or partial).",
        args.output,
        s.get("ok", 0),
        s.get("failed", 0),
    )
    if not args.no_csv:
        csv_dir = args.csv_dir if args.csv_dir is not None else args.output.parent
        write_csv_exports(
            csv_dir,
            members_raw=export_ctx.get("members_raw") or [],
            channels_merged=export_ctx.get("channels_merged") or [],
            resources=inv["resources"],
            redact=args.redact,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
