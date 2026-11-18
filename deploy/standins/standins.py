"""Deterministic stand-ins for every outside service (ADR 0007).

One listener per service, each answering from the seeded state directory with
canned data. They speak plain HTTP and JSON, not the vendor protocols: they let
a stack run with no outside account, and they never count as delivering a source
(ADR 0003). The inference stand-ins answer every request with the same canned
completion, so they say nothing about classification quality.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVICES = {
    "mailbox-mail": 9101,
    "mailbox-calendar": 9102,
    "o365-mail": 9103,
    "o365-calendar": 9104,
    "signal": 9105,
    "imessage": 9106,
    "whatsapp": 9107,
    "inference-local": 9108,
    "inference-external": 9109,
}
MANIFEST = "manifest.json"


def make_server(service: str, state: Path, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _fixture(self) -> bytes | None:
            path = state / f"{service}.json"
            return path.read_bytes() if (state / MANIFEST).exists() and path.exists() else None

        def do_GET(self) -> None:
            if self.path == "/healthz":
                ready = self._fixture() is not None
                self._send(
                    200 if ready else 503,
                    b'{"status": "ok"}\n' if ready else b'{"status": "unseeded"}\n',
                )
                return
            fixture = self._fixture()
            if self.path == "/" and fixture is not None:
                self._send(200, fixture)
            else:
                self._send(404 if fixture is not None else 503, b'{"error": "not available"}\n')

        def do_POST(self) -> None:
            fixture = self._fixture()
            if not service.startswith("inference-") or self.path != "/v1/chat/completions":
                self._send(404, b'{"error": "not available"}\n')
                return
            if fixture is None:
                self._send(503, b'{"error": "not available"}\n')
                return
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)  # the request never changes the answer
            canned = json.loads(fixture)
            answer = {
                "model": canned["model"],
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": canned["completion"]}}
                ],
            }
            self._send(200, (json.dumps(answer, sort_keys=True) + "\n").encode())

        def log_message(self, format: str, *args: object) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    state = Path(os.environ.get("STANDINS_STATE", "/state/seed"))
    servers = [make_server(name, state, "0.0.0.0", port) for name, port in SERVICES.items()]
    threads = [threading.Thread(target=server.serve_forever) for server in servers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
