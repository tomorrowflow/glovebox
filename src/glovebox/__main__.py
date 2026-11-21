"""Entry point: `glovebox` or `python -m glovebox`.

With no command it runs the service; `account` and `token` are the operator's commands.
"""

import os
import sys

from glovebox.cli import run, serve


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] in ([], ["serve"]):
        sys.exit(serve(argv or ["serve"], os.environ))
    sys.exit(run(argv, os.environ, sys.stdout, sys.stderr))


if __name__ == "__main__":
    main()
