"""Office 365 mail, read through the Microsoft Graph API with a read-only delegated login.

Sign-in and token refresh are MSAL's, Microsoft's own maintained library. The one Graph
request the gate makes is a `GET` of the messages in the inbox, over the standard library like
every other source. The login asks for one permission, `Mail.Read` (delegated), the narrowest
that returns a message body for the classifier: `Mail.ReadBasic` leaves the body out. A token
that carries it cannot send, change, move or delete mail, and a `GET` does not mark a message
read. That is provider-enforced read-only (ADR 0003), and the gate checks it at every sync: a
token that also carries a permission that could write is refused, as `blocked`.

The operator's login lives in the account's credentials file (ADR 0004): the client ID, the
authority and MSAL's token cache, which holds the refresh token. The gate rewrites that file
when MSAL refreshes the cache, because Microsoft rotates refresh tokens.

Nothing an upstream said is kept: errors carry no detail, and neither Graph's nor the
authority's error text is read into an exception, a log line or a response.
"""

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import msal

from glovebox.credentials import CredentialsError, Secret, write_credentials
from glovebox.sources import (
    BLOCKED_HTTP_STATUSES,
    FETCH_TIMEOUT_SECONDS,
    MAX_TEXT_CHARS,
    NoRedirects,
    SourceBlocked,
    SourceError,
    SourceItem,
    SourceKind,
    SourceMailbox,
    parse_received,
    read_status_of,
    sender_key_of,
)

logger = logging.getLogger(__name__)
# MSAL and the HTTP libraries under it log what an upstream answered, error descriptions
# included. None of it may reach the gate's log (ADR 0001): they log nowhere.
for _name in ("msal", "requests", "urllib3"):
    logging.getLogger(_name).addHandler(logging.NullHandler())
    logging.getLogger(_name).propagate = False

# The Graph endpoint of the public cloud; the account's source names the endpoint to use.
GRAPH_URL = "https://graph.microsoft.com/v1.0"
AUTHORITY_URL = "https://login.microsoftonline.com"
# What the login asks for. MSAL adds `openid`, `profile` and `offline_access` itself; they sign
# the operator in and keep the refresh token, and open no mailbox operation.
SCOPES = ["Mail.Read"]
# A token is used only if every permission it carries is one of these: the sign-in scopes, or
# a read permission. Anything else (`Mail.ReadWrite`, `Mail.Send`, `Directory.AccessAsUser.All`)
# means the credential could write, and the account would no longer be provider-enforced.
SIGN_IN_SCOPES = frozenset({"openid", "profile", "email", "offline_access"})
READ_SCOPE = re.compile(r"[a-z]+\.(read|readbasic)(\.(all|shared|basic))?")
# Token errors that mean the operator or the employer no longer grants access: consent was
# withdrawn or never given, the refresh token was revoked or expired, or a policy refuses it.
# Anything else (the authority being down, throttled, or answering something else) is an outage.
BLOCKING_TOKEN_ERRORS = frozenset(
    {
        "invalid_grant",
        "interaction_required",
        "consent_required",
        "invalid_client",
        "unauthorized_client",
        "access_denied",
    }
)

PAGE_SIZE = 25
MAX_PAGE_BYTES = 16 * 1024 * 1024
# The most messages one read returns. A window holding more is cut to the newest, and the
# rest is reported as skipped, so that the account is `incomplete`, not `current`.
MAX_MESSAGES = 5_000
# One read, its token and all its pages, ends by then whatever the servers do.
READ_DEADLINE_SECONDS = 60
MESSAGES_PATH = "/me/mailFolders/inbox/messages"
FIELDS = "id,conversationId,from,receivedDateTime,isRead,hasAttachments,subject,body"
# Plain text bodies, and message IDs that stay the same when a message is moved to another
# folder, so that its handle does too.
PREFER = 'outlook.body-content-type="text", IdType="ImmutableId"'


class LoginError(Exception):
    """The credentials file does not hold a usable Microsoft login. The message is fixed text."""


@dataclass(frozen=True)
class Login:
    """What the gate needs to act as the operator at Microsoft: no password, only a token cache."""

    client_id: str
    authority: str
    token_cache: str

    @classmethod
    def parse(cls, secret: Secret) -> "Login":
        try:
            document = json.loads(secret.reveal())
        except ValueError:
            raise LoginError("the credentials file is not a Microsoft login") from None
        if not isinstance(document, Mapping):
            raise LoginError("the credentials file is not a Microsoft login")
        client_id = document.get("client_id")
        authority = document.get("authority")
        token_cache = document.get("token_cache")
        if not (
            isinstance(client_id, str)
            and client_id
            and isinstance(authority, str)
            and authority
            and isinstance(token_cache, str)
            and token_cache
        ):
            raise LoginError("the credentials file is not a Microsoft login")
        if urlsplit(authority).scheme != "https":
            raise LoginError("the authority of the login must be an https URL")
        return cls(client_id, authority, token_cache)

    def text(self) -> str:
        document = {
            "client_id": self.client_id,
            "authority": self.authority,
            "token_cache": self.token_cache,
        }
        return json.dumps(document, indent=2) + "\n"


def graph_problem(location: str) -> str | None:
    """Why this is not a Graph endpoint the gate may send a token to, in fixed text, or None."""
    if urlsplit(location).scheme != "https":
        return "the Graph endpoint must be an https URL, such as " + GRAPH_URL
    return None


def granted_scopes(target: object) -> frozenset[str]:
    """The permissions of a token, as MSAL records them: a space-separated list, each either
    bare or with its resource in front (`https://graph.microsoft.com/Mail.Read`)."""
    if not isinstance(target, str):
        return frozenset()
    return frozenset(scope.rsplit("/", 1)[-1].lower() for scope in target.split())


def can_write(granted: frozenset[str]) -> bool:
    """Whether a token carries a permission that is neither sign-in nor a read permission."""
    return any(
        scope not in SIGN_IN_SCOPES and READ_SCOPE.fullmatch(scope) is None for scope in granted
    )


def read_only(granted: frozenset[str]) -> bool:
    """Whether a token is shown to be unable to write: it names its permissions, and none of
    them could. A token that names none is not shown to be anything."""
    return bool(granted) and not can_write(granted)


def token_scopes(cache: msal.SerializableTokenCache, token: str) -> frozenset[str]:
    """The permissions the authority recorded for this access token."""
    for entry in cache.search(msal.TokenCache.CredentialType.ACCESS_TOKEN):
        if entry.get("secret") == token:
            return granted_scopes(entry.get("target"))
    return frozenset()


def public_client(
    client_id: str, authority: str, cache: msal.SerializableTokenCache
) -> msal.PublicClientApplication:
    """MSAL's client for a device code login. The authority is the operator's own choice (the
    public cloud, a sovereign cloud, a federation), so MSAL's check of it against Microsoft's
    list of hosts, which would also ask Microsoft about it on every start, is off."""
    return msal.PublicClientApplication(
        client_id,
        authority=authority,
        token_cache=cache,
        timeout=FETCH_TIMEOUT_SECONDS,
        instance_discovery=False,
    )


class Session:
    """One account's access token, refreshed through its token cache."""

    def __init__(self, login: Login) -> None:
        self.login = login
        self.cache = msal.SerializableTokenCache()
        self.cache.deserialize(login.token_cache)
        self.app = public_client(login.client_id, login.authority, self.cache)

    def access_token(self) -> str:
        """A token for `Mail.Read`, from the cache or by refreshing it.

        Raises SourceBlocked if access is not granted and SourceError for any other failure.
        """
        try:
            accounts = self.app.get_accounts()
            # The `_with_error` form tells a refresh that failed (and why) from a cache with no
            # refresh token to try, which is the only case where it answers None.
            result = (
                self.app.acquire_token_silent_with_error(SCOPES, account=accounts[0])
                if len(accounts) == 1
                else None
            )
        except Exception:
            # MSAL raises whatever its HTTP and parsing layers do; none of it may carry on.
            raise SourceError from None
        if result is None:
            # Nobody signed in, or a cache that could be anyone's: not access to use.
            raise SourceBlocked
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            error = result.get("error")
            if isinstance(error, str) and error in BLOCKING_TOKEN_ERRORS:
                raise SourceBlocked
            raise SourceError
        if not read_only(token_scopes(self.cache, token)):
            raise SourceBlocked
        return token

    def forget_access_tokens(self) -> None:
        """Drop the cached access tokens, so that the next sync asks the authority for one."""
        for entry in list(self.cache.search(msal.TokenCache.CredentialType.ACCESS_TOKEN)):
            self.cache.remove_at(entry)

    def refreshed(self) -> Login | None:
        """The login with MSAL's current cache, if refreshing changed it."""
        if not self.cache.has_state_changed:
            return None
        return Login(self.login.client_id, self.login.authority, self.cache.serialize())


class O365MailSource:
    """Work mail from Microsoft Graph: the inbox messages of the last 30 days, read-only.

    The account's source is the Graph endpoint and its credentials file the login made by
    `glovebox o365-login`. A message that cannot be understood is skipped and counted.
    """

    kind: SourceKind = "o365_mail"

    def __init__(self, location: str, credentials: Path, secret: Secret) -> None:
        self._location = location
        self._credentials = credentials
        self._secret = secret

    def fetch(self, since: datetime, now: datetime) -> SourceMailbox:
        deadline = time.monotonic() + READ_DEADLINE_SECONDS
        if graph_problem(self._location) is not None:
            raise SourceBlocked  # nothing is sent: access is not granted
        try:
            session = Session(Login.parse(self._secret))
        except LoginError:
            raise SourceBlocked from None
        except Exception:
            raise SourceError from None
        try:
            token = session.access_token()
        finally:
            self._remember(session)
        try:
            return self._read(token, since, deadline)
        except SourceBlocked:
            # Graph refused a token the cache thought good: it is not handed out again.
            session.forget_access_tokens()
            self._remember(session)
            raise

    def _remember(self, session: Session) -> None:
        """Keep a refreshed token cache: Microsoft replaces the refresh token as it is used."""
        refreshed = session.refreshed()
        if refreshed is None:
            return
        try:
            write_credentials(self._credentials, refreshed.text())
        except CredentialsError:
            logger.warning("the refreshed Microsoft login could not be saved")

    def _read(self, token: str, since: datetime, deadline: float) -> SourceMailbox:
        base = self._location.rstrip("/")
        first = (
            f"{base}{MESSAGES_PATH}?$select={FIELDS}"
            f"&$filter={quote('receivedDateTime ge ' + utc_text(since))}"
            f"&$orderby={quote('receivedDateTime desc')}&$top={PAGE_SIZE}"
        )
        origin = urlsplit(base)[:2]
        items: dict[str, SourceItem] = {}
        skipped = 0
        page: str | None = first
        while page is not None:
            document = get_page(page, token, deadline)
            messages = document.get("value")
            if not isinstance(messages, list):
                raise SourceError
            for message in messages:
                item = parse_graph_message(message)
                if item is None:
                    skipped += 1
                elif item.upstream_id not in items:
                    items[item.upstream_id] = item
            page = next_page(document, origin)
            if len(items) >= MAX_MESSAGES:
                break
        kept = list(items.values())[:MAX_MESSAGES]
        if page is not None or len(kept) < len(items):
            skipped += 1  # more than the cap: at least one message is missing
        return SourceMailbox(account_key="o365", items=kept, skipped=skipped)


def utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_page(document: Mapping[str, object], origin: tuple[str, str]) -> str | None:
    """The link to the next page, if there is one. The token goes only where it went before:
    a link to another origin is a failure, not a request."""
    link = document.get("@odata.nextLink")
    if link is None:
        return None
    if not isinstance(link, str) or urlsplit(link)[:2] != origin:
        raise SourceError
    return link


def get_page(url: str, token: str, deadline: float) -> Mapping[str, object]:
    """One `GET` of a Graph URL: the JSON object it answers with.

    Raises SourceBlocked if Graph refuses the token and SourceError for any other failure.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceError
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    request.add_header("Prefer", PREFER)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects)
    try:
        with opener.open(request, timeout=min(FETCH_TIMEOUT_SECONDS, remaining)) as response:
            data = response.read(MAX_PAGE_BYTES + 1)
        document = json.loads(data) if len(data) <= MAX_PAGE_BYTES else None
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in BLOCKED_HTTP_STATUSES:
            raise SourceBlocked from None
        raise SourceError from None
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        raise SourceError from None
    if not isinstance(document, Mapping):
        raise SourceError
    return document


def parse_graph_message(message: object) -> SourceItem | None:
    """One Graph message as an item, or None if it cannot be understood.

    Only the keys named in `FIELDS` are read, and of those only an ID, the sender's address,
    the conversation ID, the time, two flags and the subject and body for the classifier.
    """
    if not isinstance(message, Mapping):
        return None
    upstream_id = message.get("id")
    received = parse_received(message.get("receivedDateTime"))
    sender = address_of(message.get("from"))
    has_attachments = message.get("hasAttachments")
    if (
        not isinstance(upstream_id, str)
        or not upstream_id
        or received is None
        or sender is None
        or not isinstance(has_attachments, bool)
    ):
        return None
    conversation = message.get("conversationId")
    return SourceItem(
        upstream_id=upstream_id,
        sender_key=sender,
        thread_key=conversation if isinstance(conversation, str) and conversation else upstream_id,
        received=received,
        read_status=read_status_of(message.get("isRead")),
        has_attachments=has_attachments,
        text=graph_text_of(message),
    )


def address_of(sender: object) -> str | None:
    """The address of Graph's `from` (`{"emailAddress": {"address": ...}}`), as a sender key."""
    if not isinstance(sender, Mapping):
        return None
    mailbox = sender.get("emailAddress")
    address = mailbox.get("address") if isinstance(mailbox, Mapping) else None
    return sender_key_of(address)


def graph_text_of(message: Mapping[str, object]) -> str:
    """The subject and the body text, those of them that are strings, cut to the limit."""
    body = message.get("body")
    content = body.get("content") if isinstance(body, Mapping) else None
    parts = [part for part in (message.get("subject"), content) if isinstance(part, str)]
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


class SignInFailed(Exception):
    """The operator's sign-in did not complete. The message is `error` and its codes, which
    Microsoft defines; it holds no token."""


def sign_in(client_id: str, authority: str, announce: Callable[[str], None]) -> Login:
    """Sign the operator in with the device code flow and return the login to store.

    Asks for `Mail.Read` and nothing else. The operator's own command, outside MCP: the
    error it reports is for the operator and is not served to anyone.
    """
    cache = msal.SerializableTokenCache()
    app = public_client(client_id, authority, cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise SignInFailed(describe(flow))
    announce(str(flow["message"]))
    result = app.acquire_token_by_device_flow(flow)
    token = result.get("access_token")
    if not isinstance(token, str):
        raise SignInFailed(describe(result))
    if not read_only(token_scopes(cache, token)):
        raise SignInFailed(
            "the login is not shown to be read-only: the app registration grants more than "
            "read access to mail (remove its write permissions in the Entra admin center, or "
            "use another registration), or the authority did not say what it grants"
        )
    return Login(client_id, authority, cache.serialize())


def describe(result: Mapping[str, object]) -> str:
    """Microsoft's error name and numeric codes from a failed token or device flow request."""
    codes = result.get("error_codes")
    numbers = (
        [str(code) for code in codes if isinstance(code, int)] if isinstance(codes, list) else []
    )
    error = result.get("error")
    name = error if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,64}", error) else "failed"
    return f"{name} (AADSTS {', '.join(numbers)})" if numbers else name
