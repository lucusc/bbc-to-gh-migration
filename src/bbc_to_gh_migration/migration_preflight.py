"""Preflight checks and GitHub destination metadata."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.request

from .migration_utils import GB, MigrationError


class PreflightMixin:
    @staticmethod
    def assert_approval(action: str, approver: str | None, reference: str | None) -> None:
        if not approver or not approver.strip():
            raise MigrationError(f"{action} requires --approver.")
        if not reference or not reference.strip():
            raise MigrationError(f"{action} requires --approval-reference.")

    def assert_command(self, name: str) -> None:
        if shutil.which(name) is None:
            raise MigrationError(f"Required command is not available on PATH: {name}")

    def assert_tls(self) -> None:
        try:
            proc = subprocess.run(
                ["git", "config", "--list", "--show-origin"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise MigrationError("Required command is not available on PATH: git") from exc
        if proc.returncode != 0:
            raise MigrationError("Unable to inspect Git configuration for TLS certificate verification.")
        for entry in proc.stdout.splitlines():
            if re.search(r"(?i)\shttp(?:\..+)?\.sslverify=false\s*$", entry):
                raise MigrationError(f"Git TLS certificate verification is disabled: {entry}")
        flag = os.environ.get("GIT_SSL_NO_VERIFY")
        if flag and flag.lower() not in ("0", "false"):
            raise MigrationError("GIT_SSL_NO_VERIFY disables TLS certificate verification.")

    def assert_free_space(self) -> None:
        probe = self.working_directory
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        if free < self.min_gb * GB:
            raise MigrationError(
                f"Insufficient free space on {probe}. Available: {free / GB:.2f} GB; "
                f"required minimum: {self.min_gb} GB."
            )
        self.log("INFO", f"Disk space on {probe}: {free / GB:.2f} GB available.")

    def github_metadata(self, repository: str) -> dict:
        if shutil.which("gh"):
            try:
                lines = self.run(["gh", "api", f"repos/{self.org}/{repository}"])
                return json.loads("\n".join(lines))
            except (MigrationError, ValueError) as exc:
                raise MigrationError(
                    f"Unable to read GitHub repository metadata for {self.org}/{repository}."
                ) from exc

        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if proc.returncode != 0:
            raise MigrationError("Unable to retrieve GitHub credentials from Git Credential Manager.")
        credential = dict(
            line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
        )
        if not credential.get("username") or not credential.get("password"):
            raise MigrationError("Git Credential Manager did not return a GitHub username and credential.")
        token = base64.b64encode(
            f"{credential['username']}:{credential['password']}".encode("ascii")
        ).decode("ascii")
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.org}/{repository}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Basic {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise MigrationError(
                f"Unable to read GitHub repository metadata for {self.org}/{repository}."
            ) from exc
        finally:
            credential.clear()
            token = ""

    def assert_destination_metadata(self) -> None:
        metadata = self.github_metadata(self.gh_repo)
        if str(metadata.get("owner", {}).get("login", "")).lower() != self.org.lower():
            raise MigrationError(f"Destination repository is not owned by {self.org}.")
        if not metadata.get("permissions", {}).get("push"):
            raise MigrationError(
                "Authenticated GitHub identity does not have push permission on the destination."
            )
