"""Service entry point: `glovebox` or `python -m glovebox`."""

import os
import sys

import uvicorn

from glovebox.config import ConfigError
from glovebox.server import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def main() -> None:
    host = os.environ.get("GLOVEBOX_HOST", DEFAULT_HOST)
    port = int(os.environ.get("GLOVEBOX_PORT", DEFAULT_PORT))
    try:
        app = create_app()
    except ConfigError as error:
        sys.exit(f"glovebox: {error}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
