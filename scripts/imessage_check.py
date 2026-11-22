#!/usr/bin/env python3
"""Ask a running gate what it serves of one iMessage account, for the operator's acceptance run.

It connects as an agent would, with an agent token, calls `list_accounts` and `list_items`
and prints only what crosses the gate: the account's source kind, read-only enforcement,
sync status and last sync time, and for each item the permitted fields. Nothing here reads
Messages or any file of the account; no message text exists in the output to print.

Usage (docs/imessage.md has the whole acceptance run):

    GLOVEBOX_CHECK_TOKEN=... .venv/bin/python scripts/imessage_check.py --url http://127.0.0.1:18765/mcp

Exit status: 0 if the account is `current`, 1 if it is not (a blocked account prints
`blocked`, which is the result to record when the device does not allow access), 2 if the
gate cannot be asked.
"""

import argparse
import asyncio
import json
import os
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

TOKEN_VAR = "GLOVEBOX_CHECK_TOKEN"


async def ask(url: str, token: str, limit: int) -> tuple[dict[str, object], dict[str, object]]:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            accounts = await client.call_tool("list_accounts", {})
            items = await client.call_tool("list_items", {"limit": limit})
    return accounts.structured_content or {}, items.structured_content or {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--url", required=True, help="the gate's MCP URL, ending in /mcp")
    parser.add_argument("--limit", type=int, default=5, help="newest items to show (at most 50)")
    args = parser.parse_args()
    token = os.environ.get(TOKEN_VAR, "")
    if not token:
        print(f"set {TOKEN_VAR} to an agent token", file=sys.stderr)
        return 2
    try:
        accounts, items = asyncio.run(ask(args.url, token, min(max(args.limit, 1), 50)))
    except Exception as error:
        # Only the type: an exception message can quote what the other end sent.
        print(f"the gate could not be asked: {type(error).__name__}", file=sys.stderr)
        return 2
    entries = items.get("accounts") or accounts.get("accounts") or []
    shown = items.get("items") or []
    print(json.dumps({"error": items.get("error"), "accounts": entries, "items": shown}, indent=2))
    current = bool(entries) and all(entry.get("status") == "current" for entry in entries)
    return 0 if current else 1


if __name__ == "__main__":
    sys.exit(main())
