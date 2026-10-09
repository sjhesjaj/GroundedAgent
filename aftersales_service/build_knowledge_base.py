"""CLI entry point that avoids re-executing the package's imported KB module."""

from .knowledge_base import main


if __name__ == "__main__":
    raise SystemExit(main())
