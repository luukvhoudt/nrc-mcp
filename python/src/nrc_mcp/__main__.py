"""Entry point: `python -m nrc_mcp`, or `mise run mcp:serve`."""

from __future__ import annotations

import sys


def main() -> int:
    # `--describe` prints the surface without starting a server, which is what
    # `mise run mcp:tools` uses and what a human wants when checking whether a tool exists.
    if "--describe" in sys.argv:
        from .server import describe_surface

        print(describe_surface())
        return 0

    from .server import main as serve

    return serve()


if __name__ == "__main__":
    sys.exit(main())
