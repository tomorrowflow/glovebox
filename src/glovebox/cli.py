"""The operator's command line: accounts and agent tokens (ADR 0004).

It runs on the gateway host and opens the database file directly. Nothing here is
reachable through MCP, and the operator needs the same file access as the service
itself. Output never holds a credential; a token value is printed once, by `token
issue`, and cannot be read back.
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from glovebox.access import OPERATIONS, Access, AccessError
from glovebox.config import ConfigError, Settings
from glovebox.credentials import CredentialsError, read_credentials
from glovebox.sources import BEARER_KINDS, SOURCE_KINDS, bearer_may_travel

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
FIXTURE_ACCOUNT = "fixture"
FIXTURE_TOKEN_VAR = "GLOVEBOX_FIXTURE_TOKEN"
FIXTURE_URL_VAR = "GLOVEBOX_STANDIN_MAILBOX_MAIL"
FIXTURE_FILE_VAR = "GLOVEBOX_MAIL_FIXTURE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glovebox",
        description="Run the gate, or set up its accounts and agent tokens on this host.",
    )
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="run the service (the default)")
    serve.add_argument(
        "--bootstrap-fixture",
        action="store_true",
        help=(
            f"first make sure a fixture account and the token in ${FIXTURE_TOKEN_VAR} exist, "
            "for the throwaway and test stacks"
        ),
    )

    account = commands.add_parser("account", help="add, list or remove accounts")
    account_commands = account.add_subparsers(dest="action", required=True)
    add = account_commands.add_parser("add", help="add an account")
    add.add_argument("name", help="the operator's own label; agents never see it")
    add.add_argument("--kind", required=True, choices=SOURCE_KINDS)
    add.add_argument("--source", required=True, help="a file path or an http(s) URL")
    add.add_argument("--credentials", type=Path, help="file holding the upstream credentials")
    account_commands.add_parser("list", help="list accounts")
    remove = account_commands.add_parser("remove", help="remove an account and its stored items")
    remove.add_argument("name")

    token = commands.add_parser("token", help="issue, list or revoke agent tokens")
    token_commands = token.add_subparsers(dest="action", required=True)
    issue = token_commands.add_parser("issue", help="issue a token; its value is shown once")
    issue.add_argument("--account", action="append", required=True, dest="accounts", metavar="NAME")
    issue.add_argument(
        "--operation",
        action="append",
        dest="operations",
        choices=OPERATIONS,
        help="limit the token to this read operation; repeat for several (default: all)",
    )
    issue.add_argument("--label", default="", help="a note for yourself")
    token_commands.add_parser("list", help="list tokens, never their values")
    revoke = token_commands.add_parser("revoke", help="revoke a token by its id")
    revoke.add_argument("id")
    return parser


def run(argv: Sequence[str], env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    """Run one operator command; the exit status. `serve` is not run here."""
    args = build_parser().parse_args(argv)
    try:
        access = Access(Settings.from_env(env).db_path)
        if args.command == "account":
            return _account(args, access, out)
        return _token(args, access, out, err)
    except (AccessError, CredentialsError, ConfigError) as error:
        print(f"glovebox: {error}", file=err)
        return 1


def _account(args: argparse.Namespace, access: Access, out: TextIO) -> int:
    if args.action == "add":
        source = _checked_source(args.source)
        if args.credentials is not None:
            # Refuse a file that is open to others now, not at the first request.
            read_credentials(args.credentials)
            if args.kind in BEARER_KINDS and urlsplit(source).scheme in ("http", "https"):
                if not bearer_may_travel(source):
                    raise AccessError(
                        "the bridge token is sent only over https or to this host: "
                        "use an https source"
                    )
        account = access.add_account(args.name, args.kind, source, args.credentials)
        print(f"added account {account.name} with handle {account.handle}", file=out)
    elif args.action == "list":
        for account in access.accounts():
            status = "-"
            if account.credentials is not None:
                try:
                    read_credentials(account.credentials)
                    status = "ok"
                except CredentialsError as error:
                    status = str(error)
            fields = (
                account.name,
                account.handle,
                account.kind,
                account.source,
                str(account.credentials or "-"),
                status,
            )
            print("\t".join(fields), file=out)
    else:
        access.remove_account(args.name)
        print(f"removed account {args.name}", file=out)
    return 0


def _token(args: argparse.Namespace, access: Access, out: TextIO, err: TextIO) -> int:
    if args.action == "issue":
        operations = args.operations or OPERATIONS
        value = access.issue_token(args.accounts, operations, args.label)
        print(value, file=out)
        print(
            "glovebox: the line above is the token. It is shown once and cannot be recovered; "
            "revoke it and issue another if it is lost.",
            file=err,
        )
    elif args.action == "list":
        for record in access.tokens():
            fields = (
                record.id,
                f"revoked {record.revoked}" if record.revoked else "active",
                record.created,
                ",".join(record.accounts) or "-",
                ",".join(record.operations) or "-",
                record.label or "-",
            )
            print("\t".join(fields), file=out)
    else:
        if access.revoke_token(args.id):
            print(f"revoked token {args.id}", file=out)
        else:
            print(f"token {args.id} was already revoked", file=out)
    return 0


def _checked_source(source: str) -> str:
    """A URL without anything that could be a credential, or an absolute file path."""
    parts = urlsplit(source)
    if parts.scheme in ("http", "https"):
        if parts.username or parts.password or parts.query or parts.fragment:
            raise AccessError("keep credentials out of the source URL; use --credentials")
        return source
    return os.path.abspath(source)


def bootstrap_fixture(env: Mapping[str, str]) -> None:
    """Register the fixture stack's account and token before the service starts.

    The throwaway and test stacks hold only fictional data. Their tooling hands the gate
    a token in the environment; only its digest is stored. Safe to repeat on every start,
    and a token the operator revoked stays revoked.
    """
    token = env.get(FIXTURE_TOKEN_VAR, "")
    url, file = env.get(FIXTURE_URL_VAR), env.get(FIXTURE_FILE_VAR)
    if (url is None) == (file is None):
        raise ConfigError(f"set exactly one of {FIXTURE_URL_VAR} and {FIXTURE_FILE_VAR}")
    source = url if url is not None else str(file)
    Access(Settings.from_env(env).db_path).ensure_fixture(
        FIXTURE_ACCOUNT, "fixture_mail", _checked_source(source), token
    )


def serve(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run the service. Imported late: the operator's commands do not need the web stack."""
    args = build_parser().parse_args(argv)
    try:
        if args.bootstrap_fixture:
            bootstrap_fixture(env)
        import uvicorn

        from glovebox.server import create_app

        settings = Settings.from_env(env)
        app = create_app(settings)
        refused = unusable_accounts(Access(settings.db_path))
    except (AccessError, ConfigError) as error:
        print(f"glovebox: {error}", file=sys.stderr)
        return 1
    for name, reason in refused:
        print(f"glovebox: account {name} is not served: {reason}", file=sys.stderr)
    uvicorn.run(
        app,
        host=env.get("GLOVEBOX_HOST", DEFAULT_HOST),
        port=int(env.get("GLOVEBOX_PORT", DEFAULT_PORT)),
    )
    return 0


def unusable_accounts(access: Access) -> list[tuple[str, str]]:
    """The accounts whose credentials file the gate refuses to use, with the reason.

    They are refused on every request too; this tells the operator at start.
    """
    refused = []
    for account in access.accounts():
        if account.credentials is None:
            continue
        try:
            read_credentials(account.credentials)
        except CredentialsError as error:
            refused.append((account.name, str(error)))
    return refused
