"""Logging and safe native command execution."""
from __future__ import annotations

import os
import subprocess
import sys

from migration_utils import MigrationError, now, to_safe_text


class ExecutionMixin:
    def log(self, level: str, message: str) -> None:
        entry = f"{now()} [{level}] {to_safe_text(message)}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        print(entry, file=sys.stderr, flush=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(entry + "\n")

    def run(self, args: list[str], *, env: dict[str, str] | None = None) -> list[str]:
        """Run a command without a shell; returns safe output lines."""
        self.log("INFO", "Running: " + " ".join(to_safe_text(a) for a in args))
        full_env = {**os.environ, **env} if env else None
        try:
            proc = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=full_env,
            )
        except FileNotFoundError as exc:
            raise MigrationError(f"Required command is not available on PATH: {args[0]}") from exc
        lines = [to_safe_text(line) for line in proc.stdout.splitlines()]
        for line in lines:
            if line:
                self.log("INFO", line)
        if proc.returncode != 0:
            raise MigrationError(
                f"Command failed with exit code {proc.returncode}: "
                + " ".join(to_safe_text(a) for a in args)
            )
        return lines

