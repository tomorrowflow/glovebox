"""Inference backends of the quarantined classifier (ADR 0002, ticket 12).

The gate runs against the canned inference stand-ins of the test tier (`deploy/standins`),
started in this process: one plays the local runtime, one the external provider. Each
answers with a completion the test chooses, so a test decides what the "model" says, and
each records what it was sent, so a test can tell which of them an item's text reached.
Error shapes a stand-in cannot make (an outage mid-reply, a redirect, a slow answer) come
from a small scripted server. Nothing here says anything about a real model's quality.
"""

import http.client
import io
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from glovebox import inference
from glovebox.classification import ClassifierUnavailable, Topic
from glovebox.classifier_config import (
    DEFAULT_LOCAL_MODEL,
    DEFAULT_LOCAL_URL,
    Endpoint,
    parse_classifier_config,
)
from glovebox.config import ConfigError
from glovebox.inference import InferenceClassifier
from tests.client import ACCOUNTS_TOOL, agent_client
from tests.conftest import GateStarter, clean_environment, free_port
from tests.hostile import CORPUS, assert_no_corpus, hostile_mail_fixture, observe_gate
from tests.test_classifier import (
    GOOD,
    GOOD_SERVED,
    RANGE,
    TOPICS,
    UNKNOWN_FIELDS,
    call,
    hostile_outputs,
    list_items,
    mailbox,
    message,
    served,
)
from tests.test_gate import storage_files, write_fixture
from tests.test_standins import load_standins

HOME_BODY = "HOMEBODY-body about the invoice"
WORK_BODY = "WORKBODY-body about the invoice"
BILLING = json.dumps(GOOD)
TRAVEL = json.dumps({**GOOD, "topic": "travel"})
CLASSIFIED = {**GOOD_SERVED, "status": "classified"}
CLASSIFIED_TRAVEL = {**CLASSIFIED, "topic": "travel"}
UNAVAILABLE = {**UNKNOWN_FIELDS, "status": "unavailable"}
TOPIC_LIST = [Topic("billing", "Invoices", "Money owed."), Topic("travel", "Trips", "")]


# --- the stand-ins ----------------------------------------------------------------------


@dataclass(frozen=True)
class Seen:
    """One request a stand-in received."""

    path: str
    headers: Mapping[str, str]
    body: bytes

    @property
    def json(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.body)
        return document


class Standin:
    def __init__(self, url: str, seen: list[Seen]) -> None:
        self.url = url
        self.seen = seen

    def texts(self) -> list[str]:
        """The text of the item each request asked about."""
        return [request.json["messages"][-1]["content"] for request in self.seen]


def seed_state(directory: Path, local: str, external: str) -> Path:
    """A seeded state directory whose two inference stand-ins answer with these completions."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "inference-local.json").write_text(
        json.dumps({"model": "stub-local", "completion": local})
    )
    (directory / "inference-external.json").write_text(
        json.dumps({"model": "stub-external", "completion": external})
    )
    (directory / "manifest.json").write_text("{}")
    return directory


@contextmanager
def standin(service: str, state: Path) -> Iterator[Standin]:
    """The stand-in for `service` (`inference-local` or `inference-external`), recording."""
    server = load_standins().make_server(service, state, "127.0.0.1", 0)
    handler = server.RequestHandlerClass
    original = handler.do_POST
    seen: list[Seen] = []

    def recording(self: Any) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        seen.append(Seen(self.path, dict(self.headers.items()), body))
        self.rfile = io.BytesIO(body)  # the stand-in reads the body again, as it always does
        original(self)

    setattr(handler, "do_POST", recording)  # noqa: B010 - patching one server's own handler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Standin(f"http://127.0.0.1:{server.server_port}", seen)
    finally:
        server.shutdown()
        server.server_close()


def closed_address() -> str:
    """An address on this host where nothing listens: an inference server that is down."""
    return f"http://127.0.0.1:{free_port()}"


class Scripted(ThreadingHTTPServer):
    """A server that answers every request the same, odd way, and counts them."""

    def __init__(self, status: int, body: bytes, headers: Mapping[str, str], delay: float) -> None:
        self.hits: list[str] = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.do_POST()

            def do_POST(self) -> None:
                hits.append(self.command)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                time.sleep(delay)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


@contextmanager
def scripted(
    status: int = 200,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
    delay: float = 0,
) -> Iterator[Scripted]:
    server = Scripted(status, body, headers or {}, delay)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def completion(content: object) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode()


# --- the operator's side ----------------------------------------------------------------


class Operator:
    """The classifier configuration and the environment the gate is started with."""

    def __init__(self, directory: Path) -> None:
        self.config = directory / "classifier.json"
        self.inference: dict[str, object] = {}
        self.local: str | None = None  # the stand-in address the stack hands the gate
        self.external: str | None = None

    def env(self) -> dict[str, str]:
        document: dict[str, object] = {"topics": TOPICS, "deadline_range": RANGE}
        if self.inference:
            document["inference"] = self.inference
        self.config.write_text(json.dumps(document))
        env = {"GLOVEBOX_CLASSIFIER_CONFIG": str(self.config)}
        if self.local:
            env["GLOVEBOX_STANDIN_INFERENCE_LOCAL"] = self.local
        if self.external:
            env["GLOVEBOX_STANDIN_INFERENCE_EXTERNAL"] = self.external
        return env


def two_accounts(tmp_path: Path) -> dict[str, Path]:
    return {
        "home": write_fixture(tmp_path / "home.json", mailbox(message("h1", HOME_BODY, 1))),
        "work": write_fixture(tmp_path / "work.json", mailbox(message("w1", WORK_BODY, 2))),
    }


async def classify_each(
    start_gate: GateStarter, tmp_path: Path, operator: Operator
) -> tuple[dict[str, dict[str, Any]], Path]:
    """Start a gate with the accounts `home` and `work`, each with its own token, and
    list each one's one item. Returns the served fields by account, and the gate's log."""
    accounts = two_accounts(tmp_path)
    tokens = {"home": ["home"], "work": ["work"]}
    results: dict[str, dict[str, Any]] = {}
    with start_gate(accounts=accounts, tokens=tokens, extra_env=operator.env()) as gate:
        for name in accounts:
            async with agent_client(gate, gate.tokens[name]) as client:
                [item] = await list_items(client)
                results[name] = served(item)
    return results, gate.log


# --- local by default, external by opt-in -----------------------------------------------


@pytest.mark.anyio
async def test_with_no_inference_configuration_every_account_uses_local_inference(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = seed_state(tmp_path / "state", BILLING, TRAVEL)
    with standin("inference-local", state) as local, standin("inference-external", state) as ext:
        operator = Operator(tmp_path)
        operator.local, operator.external = local.url, ext.url

        results, _ = await classify_each(start_gate, tmp_path, operator)

    assert results == {"home": CLASSIFIED, "work": CLASSIFIED}
    assert sorted(text for text in local.texts()) == sorted(
        [f"Subject h1\n\n{HOME_BODY}", f"Subject w1\n\n{WORK_BODY}"]
    )
    assert ext.seen == []


@pytest.mark.anyio
async def test_external_inference_is_used_only_for_the_account_the_operator_enabled(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = seed_state(tmp_path / "state", BILLING, TRAVEL)
    with standin("inference-local", state) as local, standin("inference-external", state) as ext:
        operator = Operator(tmp_path)
        operator.inference = {"external": {"model": "provider-model", "accounts": ["work"]}}
        operator.local, operator.external = local.url, ext.url

        results, _ = await classify_each(start_gate, tmp_path, operator)

    assert results == {"home": CLASSIFIED, "work": CLASSIFIED_TRAVEL}
    # The text of each account reached its own backend and never the other one.
    assert local.texts() == [f"Subject h1\n\n{HOME_BODY}"]
    assert ext.texts() == [f"Subject w1\n\n{WORK_BODY}"]
    assert ext.seen[0].json["model"] == "provider-model"


@pytest.mark.anyio
@pytest.mark.parametrize("down", ["local", "external"])
async def test_when_an_accounts_backend_is_down_its_fields_are_unknown_and_nothing_goes_elsewhere(
    down: str, start_gate: GateStarter, tmp_path: Path
) -> None:
    state = seed_state(tmp_path / "state", BILLING, TRAVEL)
    with standin("inference-local", state) as local, standin("inference-external", state) as ext:
        operator = Operator(tmp_path)
        operator.inference = {"external": {"model": "provider-model", "accounts": ["work"]}}
        operator.local, operator.external = local.url, ext.url
        if down == "local":
            operator.local = closed_address()
        else:
            operator.external = closed_address()

        results, log = await classify_each(start_gate, tmp_path, operator)

    if down == "local":
        # Home's backend is down; work's answers. Home's text is not sent to work's backend.
        assert results == {"home": UNAVAILABLE, "work": CLASSIFIED_TRAVEL}
        assert local.seen == []
        assert ext.texts() == [f"Subject w1\n\n{WORK_BODY}"]
    else:
        assert results == {"home": CLASSIFIED, "work": UNAVAILABLE}
        assert ext.seen == []
        assert local.texts() == [f"Subject h1\n\n{HOME_BODY}"]
    assert_no_corpus({"log": log.read_text()})
    assert "HOMEBODY" not in log.read_text() and "WORKBODY" not in log.read_text()


@pytest.mark.anyio
async def test_unavailable_inference_is_tried_again_when_the_backend_is_back(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = seed_state(tmp_path / "state", BILLING, TRAVEL)
    accounts = two_accounts(tmp_path)
    operator = Operator(tmp_path)
    operator.local = closed_address()
    with start_gate(accounts=accounts, extra_env=operator.env()) as gate:
        async with agent_client(gate) as client:
            down = [served(item) for item in await list_items(client)]
    assert down == [UNAVAILABLE, UNAVAILABLE]

    with standin("inference-local", state) as local:
        operator.local = local.url
        with start_gate(accounts=accounts, extra_env=operator.env()) as gate:
            async with agent_client(gate) as client:
                back = [served(item) for item in await list_items(client)]

    assert back == [CLASSIFIED, CLASSIFIED]


@pytest.mark.anyio
async def test_changing_an_accounts_inference_settings_classifies_only_its_items_again(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = seed_state(tmp_path / "state", BILLING, TRAVEL)
    accounts = two_accounts(tmp_path)
    with standin("inference-local", state) as local, standin("inference-external", state) as ext:
        operator = Operator(tmp_path)
        operator.local, operator.external = local.url, ext.url
        external: dict[str, object] = {"model": "provider-model", "accounts": ["work"]}

        async def run(**inference: object) -> tuple[int, int]:
            """Restart on the same database, list both accounts, and say how many requests
            each stand-in received."""
            operator.inference = {"external": external, **inference}
            before = len(local.seen), len(ext.seen)
            with start_gate(
                accounts=accounts, tokens={"all": ["home", "work"]}, extra_env=operator.env()
            ) as gate:
                async with agent_client(gate, gate.tokens["all"]) as client:
                    assert len(await list_items(client)) == 2
            return len(local.seen) - before[0], len(ext.seen) - before[1]

        assert await run() == (1, 1)
        assert await run() == (0, 0)  # a restart changes nothing the classifications depend on

        external["model"] = "other-provider-model"
        assert await run() == (0, 1)  # the external settings: only the external account

        external["accounts"] = []
        assert await run() == (1, 0)  # work back on local inference: it is classified again
        assert await run() == (0, 0)

        assert await run(local={"model": "another-local-model"}) == (2, 0)  # local: both now
        assert await run(local={"model": "another-local-model"}) == (0, 0)


@pytest.mark.anyio
async def test_the_model_is_asked_for_one_items_text_with_no_tools_and_no_credentials(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = seed_state(tmp_path / "state", BILLING, TRAVEL)
    key = tmp_path / "provider.key"
    key.write_text("provider-secret-key\n")
    key.chmod(0o600)
    with standin("inference-local", state) as local, standin("inference-external", state) as ext:
        operator = Operator(tmp_path)
        operator.inference = {
            "external": {"model": "m", "accounts": ["work"], "api_key_file": str(key)}
        }
        operator.local, operator.external = local.url, ext.url

        await classify_each(start_gate, tmp_path, operator)

    for backend, text in ((local, HOME_BODY), (ext, WORK_BODY)):
        [request] = backend.seen
        document = request.json
        assert request.path == "/v1/chat/completions"
        assert set(document) <= {
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "stream",
            "response_format",
        }
        assert [message_["role"] for message_ in document["messages"]] == ["system", "user"]
        assert document["messages"][1]["content"].endswith(text)
        # The topic list briefs the model; no other item's text is in the request.
        assert "billing" in document["messages"][0]["content"]
        assert "Invoices and payments" in document["messages"][0]["content"]
        assert "WORKBODY" not in request.body.decode() or text == WORK_BODY
        assert "HOMEBODY" not in request.body.decode() or text == HOME_BODY
    # A key goes to the external provider alone, and no source credential goes anywhere.
    assert ext.seen[0].headers["Authorization"] == "Bearer provider-secret-key"
    assert "Authorization" not in local.seen[0].headers
    assert b"provider-secret-key" not in local.seen[0].body


# --- the model's reply is untrusted -----------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("shape", range(4))
async def test_a_reply_made_of_hostile_corpus_strings_is_validated_and_reaches_no_channel(
    shape: int, start_gate: GateStarter, tmp_path: Path
) -> None:
    _, output = hostile_outputs()[shape]
    reply = output if isinstance(output, str) else json.dumps(output)
    state = seed_state(tmp_path / "state", reply, reply)
    fixture = write_fixture(tmp_path / "hostile.json", hostile_mail_fixture())
    with standin("inference-local", state) as local:
        operator = Operator(tmp_path)
        operator.local = local.url

        with start_gate(mail_fixture=fixture, extra_env=operator.env()) as gate:
            channels = await observe_gate(gate)
            async with agent_client(gate) as client:
                items = await list_items(client)

    assert len(items) == len(CORPUS)
    assert {item["topic"] for item in items} <= {"billing", "travel", "other", "unknown"}
    assert {item["reply_needed"] for item in items} <= {"yes", "no", "unknown"}
    assert {item["action_requested"] for item in items} <= {"yes", "no", "unknown"}
    assert {item["classification_status"] for item in items} <= {"classified", "partial", "invalid"}
    observed: dict[str, str | bytes] = {**channels, "log": gate.log.read_text()}
    observed.update({f"stored {name}": data for name, data in storage_files(gate.db).items()})
    assert_no_corpus(observed)


@pytest.mark.anyio
async def test_a_reply_goes_through_validation_unchanged(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    # Fenced or padded JSON is prose to the validator: the backend does not repair a reply.
    state = seed_state(tmp_path / "state", "```json\n" + BILLING + "\n```", TRAVEL)
    with standin("inference-local", state) as local:
        operator = Operator(tmp_path)
        operator.local = local.url

        results, _ = await classify_each(start_gate, tmp_path, operator)

    assert results == {"home": {**UNKNOWN_FIELDS, "status": "invalid"}} | {
        "work": {**UNKNOWN_FIELDS, "status": "invalid"}
    }


@pytest.mark.anyio
async def test_the_accounts_tool_is_not_a_way_to_learn_the_inference_settings(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = seed_state(tmp_path / "state", BILLING, TRAVEL)
    with standin("inference-local", state) as local, standin("inference-external", state) as ext:
        operator = Operator(tmp_path)
        operator.inference = {"external": {"model": "provider-model", "accounts": ["work"]}}
        operator.local, operator.external = local.url, ext.url
        with start_gate(accounts=two_accounts(tmp_path), extra_env=operator.env()) as gate:
            async with agent_client(gate) as client:
                listed = await call(client, ACCOUNTS_TOOL)

    text = json.dumps(listed)
    for secret in ("provider-model", "external", local.url, ext.url, "stub-"):
        assert secret not in text


# --- the backend on its own -------------------------------------------------------------


def stub_state(tmp_path: Path, reply: str = BILLING) -> Path:
    return seed_state(tmp_path / "state", reply, reply)


def endpoint(url: str, key_file: Path | None = None) -> Endpoint:
    return Endpoint(url, "some-model", key_file)


def test_a_reply_is_returned_exactly_as_the_model_wrote_it(tmp_path: Path) -> None:
    reply = '  {"topic": "billing"} trailing prose ‮'
    with standin("inference-local", stub_state(tmp_path, reply)) as server:
        assert InferenceClassifier(endpoint(server.url), "local").classify("t", TOPIC_LIST) == reply


def test_a_topic_list_with_braces_still_briefs_the_model(tmp_path: Path) -> None:
    topics = [Topic("billing", "{name} {0}", "{description}")]
    with standin("inference-local", stub_state(tmp_path)) as server:
        InferenceClassifier(endpoint(server.url), "local").classify("t", topics)

    assert "{name} {0}. {description}" in server.seen[0].json["messages"][0]["content"]


def secret_key_file(tmp_path: Path, mode: int = 0o600) -> Path:
    key = tmp_path / "key"
    key.write_text("KEYMATERIAL-9f3a\n")
    key.chmod(mode)
    return key


def test_a_key_file_open_to_others_is_not_used_and_nothing_is_sent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    key = secret_key_file(tmp_path, 0o644)
    with standin("inference-external", stub_state(tmp_path)) as server:
        backend = InferenceClassifier(endpoint(server.url, key), "external")
        with pytest.raises(ClassifierUnavailable):
            backend.classify("t", TOPIC_LIST)

    assert server.seen == []
    assert "KEYMATERIAL" not in caplog.text


def test_the_environments_proxy_settings_do_not_redirect_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, closed_address())
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    with standin("inference-local", stub_state(tmp_path)) as server:
        reply = InferenceClassifier(endpoint(server.url), "local").classify("t", TOPIC_LIST)

    assert reply == BILLING
    assert len(server.seen) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_a_failure_and_nothing_is_sent_on(status: int) -> None:
    with scripted(body=completion(TRAVEL)) as target:
        with scripted(status, b"", {"Location": target.url + inference.CHAT_PATH}) as redirecting:
            backend = InferenceClassifier(endpoint(redirecting.url), "local")
            with pytest.raises(ClassifierUnavailable):
                backend.classify("t", TOPIC_LIST)

    assert redirecting.hits == ["POST"]
    assert target.hits == []


def hostile_text() -> str:
    return " ".join(CORPUS.values())


FAILURES: dict[str, dict[str, Any]] = {
    "server error": {"status": 500, "body": hostile_text().encode()},
    "not found": {"status": 404, "body": hostile_text().encode()},
    "not json": {"body": hostile_text().encode()},
    "no choices": {"body": json.dumps({"choices": [], "echo": hostile_text()}).encode()},
    "no text": {"body": completion({"text": hostile_text()})},
    "null text": {"body": completion(None)},
    "oversized": {"body": completion("x" * (inference.MAX_REPLY_BYTES + 1))},
    "invalid utf-8": {"body": b"\xff\xfe" + hostile_text().encode()},
}


@pytest.mark.parametrize("failure", FAILURES)
def test_a_failing_backend_is_unavailable_and_logs_no_text_at_any_level(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    with scripted(**FAILURES[failure]) as server:
        backend = InferenceClassifier(endpoint(server.url), "local")
        with pytest.raises(ClassifierUnavailable) as raised:
            backend.classify(hostile_text(), TOPIC_LIST)

    assert_no_corpus({"log": caplog.text, "exception": repr(raised.value)})


def test_a_server_that_is_not_there_is_unavailable_and_logs_no_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    backend = InferenceClassifier(endpoint(closed_address()), "local")

    with pytest.raises(ClassifierUnavailable):
        backend.classify(hostile_text(), TOPIC_LIST)

    assert_no_corpus({"log": caplog.text})
    assert "local inference failed" in caplog.text


def test_a_server_that_answers_too_slowly_is_unavailable_and_logs_no_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(inference, "TIMEOUT_SECONDS", 0.2)
    with scripted(body=completion(BILLING), delay=1.0) as server:
        backend = InferenceClassifier(endpoint(server.url), "local")
        with pytest.raises(ClassifierUnavailable):
            backend.classify(hostile_text(), TOPIC_LIST)

    assert_no_corpus({"log": caplog.text})


@contextmanager
def serving(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    """The address of a server whose requests `handler` answers."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


# What a trickling server starts its reply with: the body of a long reply, the middle of a
# header line, and the size line of a chunk. Each is followed by one byte every 50 ms.
TRICKLES = {
    "body": b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n",
    "header": b"HTTP/1.1 200 OK\r\nX-Pad: ",
    "chunk size": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
}


@contextmanager
def trickling(start: bytes) -> Iterator[str]:
    """A server that sends `start` and then one byte every 50 ms for a long while: each wait
    is short, the whole reply is long."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                self.wfile.write(start)
                for _ in range(200):
                    self.wfile.write(b"a")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass  # the client gave up

        def log_message(self, format: str, *args: object) -> None:
            pass

    with serving(Handler) as url:
        yield url


@pytest.mark.parametrize("phase", TRICKLES)
def test_a_reply_that_keeps_trickling_in_is_cut_off_at_one_deadline_for_the_whole_call(
    phase: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(inference, "TIMEOUT_SECONDS", 0.5)
    with trickling(TRICKLES[phase]) as url:
        backend = InferenceClassifier(endpoint(url), "local")
        started = time.monotonic()
        with pytest.raises(ClassifierUnavailable):
            backend.classify(hostile_text(), TOPIC_LIST)
        elapsed = time.monotonic() - started

    # Every single wait is under the timeout; only a deadline on the whole call ends it,
    # whichever part of the reply is the one trickling.
    assert elapsed < 2.0
    assert_no_corpus({"log": caplog.text})


def test_a_keep_alive_chunked_reply_is_read_whole() -> None:
    """The way a real runtime answers: HTTP/1.1, a chunked body, the connection kept open."""
    reply = completion(BILLING)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(reply), 20):
                piece = reply[start : start + 20]
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

        def log_message(self, format: str, *args: object) -> None:
            pass

    with serving(Handler) as url:
        assert InferenceClassifier(endpoint(url), "local").classify("t", TOPIC_LIST) == BILLING


def test_a_connect_that_takes_too_long_is_cut_off_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name lookup or a connect has no deadline of its own that the call could share. One
    that completes after the call has given up must not then send the item's text."""
    connect = http.client.HTTPConnection.connect

    def slow_connect(self: http.client.HTTPConnection) -> None:
        time.sleep(1.5)  # the name lookup or handshake that outlives the deadline
        connect(self)

    monkeypatch.setattr(inference, "TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", slow_connect)
    with standin("inference-local", stub_state(tmp_path)) as server:
        backend = InferenceClassifier(endpoint(server.url), "local")
        started = time.monotonic()
        with pytest.raises(ClassifierUnavailable):
            backend.classify("t", TOPIC_LIST)
        elapsed = time.monotonic() - started
        time.sleep(2.0)  # long enough for the connect to complete, and a request to follow it

    assert elapsed < 1.2
    assert server.seen == []


def test_a_server_that_accepts_and_then_says_nothing_is_cut_off_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inference, "TIMEOUT_SECONDS", 0.5)
    with scripted(body=completion(BILLING), delay=5.0) as server:
        backend = InferenceClassifier(endpoint(server.url), "local")
        started = time.monotonic()
        with pytest.raises(ClassifierUnavailable):
            backend.classify("t", TOPIC_LIST)

    assert time.monotonic() - started < 2.0


def test_the_request_is_not_logged_even_when_it_succeeds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    with standin("inference-local", stub_state(tmp_path, hostile_text())) as server:
        InferenceClassifier(endpoint(server.url), "local").classify(hostile_text(), TOPIC_LIST)

    assert_no_corpus({"log": caplog.text})


# --- the operator's configuration -------------------------------------------------------


def test_a_configuration_without_inference_settings_means_local_inference() -> None:
    config = parse_classifier_config({"topics": TOPICS})

    assert config.backend == "local"
    assert config.local == Endpoint(DEFAULT_LOCAL_URL, DEFAULT_LOCAL_MODEL)
    assert config.external is None
    assert config.route("work") == "local"


def test_only_the_listed_accounts_route_to_external_inference() -> None:
    config = parse_classifier_config(
        {
            "inference": {
                "external": {
                    "url": "https://inference.example.test",
                    "model": "m",
                    "accounts": ["work"],
                }
            }
        }
    )

    assert [config.route(name) for name in ("work", "home", "work2")] == [
        "external",
        "local",
        "local",
    ]


def test_an_account_digest_covers_its_own_inference_settings_and_no_others() -> None:
    def digests(**inference: object) -> tuple[str, str]:
        config = parse_classifier_config(
            {
                "inference": {
                    "external": {"url": "https://a.example.test", "model": "m"},
                    **inference,
                }
            }
        )
        return config.digest("work"), config.digest("home")

    work, home = digests(**{"external": {"url": "https://a.example.test", "model": "m"}})
    only_work = {"url": "https://a.example.test", "model": "m", "accounts": ["work"]}

    # Opting `work` in changes its digest and not `home`'s.
    assert digests(external=only_work)[0] != work
    assert digests(external=only_work)[1] == home
    # With `work` opted in, its external model is its own; the local one is not.
    moved = digests(external={**only_work, "model": "n"})
    assert moved[0] != digests(external=only_work)[0]
    assert moved[1] == home
    local_moved = digests(external=only_work, local={"model": "n"})
    assert local_moved[0] == digests(external=only_work)[0]
    assert local_moved[1] != home


@pytest.mark.parametrize(
    "inference",
    [
        {"local": {"url": "https://inference.example.test"}},  # local must be on this host
        {"local": {"url": "http://192.0.2.1:11434"}},
        {"local": {"url": "http://localhost.example.test:11434"}},
        {"local": {"url": "http://user:pass@127.0.0.1:11434"}},
        {"local": {"url": "http://127.0.0.1:11434/?q=1"}},
        {"local": {"url": "ftp://127.0.0.1"}},
        {"local": {"url": "http://127.0.0.1:99999"}},
        {"local": {"model": "bad model"}},
        {"local": {"model": "gpt-oss:120b-cloud"}},  # a cloud tag runs off the host
        {"local": {"model": "kimi-k2.7-code:cloud"}},
        {"local": {"model": "some-model:Cloud"}},
        {"local": {"model": CORPUS["injection"]}},
        {"local": {"unknown": 1}},
        {"external": {"url": "http://inference.example.test", "model": "m"}},  # https only
        {"external": {"url": "https://inference.example.test"}},  # a model is required
        {"external": {"model": "m", "accounts": ["Not A Name"]}},
        {"external": {"model": "m", "accounts": "work"}},
        {"external": {"model": "m", "api_key_file": ""}},
        {"external": {"model": "m", "extra": 1}},
        {"backend": "stub", "external": {"model": "m"}},
        {"backend": "stub", "local": {}},
        {"backend": "cloud"},
        {"backend": None},
    ],
)
def test_an_unusable_inference_setting_is_refused_without_echoing_it(inference: object) -> None:
    with pytest.raises(ConfigError) as raised:
        parse_classifier_config({"inference": inference})

    assert CORPUS["injection"] not in str(raised.value)


def test_loopback_addresses_are_local_and_an_external_one_may_be_loopback_over_http() -> None:
    for url in ("http://127.0.0.1:11434", "http://localhost:1234/", "http://[::1]:8080"):
        assert parse_classifier_config({"inference": {"local": {"url": url}}}).local.url == (
            url.rstrip("/")
        )
    external = {"url": "http://127.0.0.1:9109", "model": "m", "accounts": ["work"]}
    assert parse_classifier_config({"inference": {"external": external}}).route("work") == (
        "external"
    )


def test_listing_an_account_for_external_inference_needs_an_address_for_it() -> None:
    config = parse_classifier_config({"inference": {"external": {"model": "m", "accounts": ["a"]}}})

    with pytest.raises(ConfigError):
        config.resolved(None, None)
    assert config.resolved(None, "http://standins:9109").external == Endpoint(
        "http://standins:9109", "m"
    )


def test_the_stand_in_addresses_replace_the_configured_ones() -> None:
    config = parse_classifier_config({"inference": {"external": {"model": "m"}}})

    resolved = config.resolved("http://standins:9108/", "http://standins:9109")

    assert resolved.local.url == "http://standins:9108"
    assert resolved.external is not None and resolved.external.url == "http://standins:9109"
    with pytest.raises(ConfigError):
        config.resolved("not a url", None)


def run_gate_process(
    tmp_path: Path, document: object, **env: str
) -> subprocess.CompletedProcess[str]:
    config = tmp_path / "classifier.json"
    config.write_text(json.dumps(document))
    return subprocess.run(
        [sys.executable, "-m", "glovebox"],
        env=clean_environment(
            tmp_path / "glovebox.db", GLOVEBOX_CLASSIFIER_CONFIG=str(config), **env
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_the_service_refuses_to_start_with_an_unusable_key_file(tmp_path: Path) -> None:
    key = secret_key_file(tmp_path, 0o644)
    external = {"url": "https://inference.example.test", "model": "m", "api_key_file": str(key)}

    result = run_gate_process(tmp_path, {"inference": {"external": external}})

    assert result.returncode != 0
    assert "KEYMATERIAL" not in result.stdout + result.stderr


def test_the_service_refuses_to_start_when_an_external_account_has_no_address(
    tmp_path: Path,
) -> None:
    result = run_gate_process(
        tmp_path, {"inference": {"external": {"model": "m", "accounts": ["work"]}}}
    )

    assert result.returncode != 0
    assert "inference.external" in result.stderr


def test_the_default_local_address_is_this_hosts_loopback() -> None:
    assert urlsplit(DEFAULT_LOCAL_URL).hostname == "127.0.0.1"
