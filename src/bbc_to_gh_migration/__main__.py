"""Run the migration command with ``python -m bbc_to_gh_migration``."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
