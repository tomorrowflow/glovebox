"""Service entry point: `glovebox` or `python -m glovebox`."""

import os

import uvicorn

from glovebox.server import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def main() -> None:
    host = os.environ.get("GLOVEBOX_HOST", DEFAULT_HOST)
    port = int(os.environ.get("GLOVEBOX_PORT", DEFAULT_PORT))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
