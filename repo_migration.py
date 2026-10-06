#!/usr/bin/env python3
"""Migrate a private Bitbucket Cloud repository to a private GitHub repository.

Runs on Windows and Linux with Python 3.10+ and only the standard library.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PRE_PUSH_STATES = {"NotStarted", "CloneCompleted", "LfsFetched"}
POST_PUSH_STATES = {
    "PushStarted",
    "GitPushCompleted",
    "LfsPushCompleted",
    "VerificationFailed",
    "Verified",
    "SignedOff",
}
GB = 1024**3
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ORG_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


class MigrationError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_safe_text(text: str | None) -> str:
    if text is None:
        return ""
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1", text)
    text = re.sub(r"(?i)(authorization\s*:\s*)(basic|bearer)\s+\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\b(ghp|github_pat|bbp)_[A-Za-z0-9_]+\b", "[REDACTED]", text)
    return text


def assert_safe_name(name: str, field: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.startswith(".") or name.endswith(".git"):
        raise MigrationError(f"{field} contains unsupported characters or formatting: {name}")


def parse_ref_lines(lines: list[str]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for line in lines:
        match = re.match(r"^([0-9a-fA-F]{40,64})\s+(.+)$", line)
        if not match:
            raise MigrationError(f"Unexpected git ls-remote output: {line}")
        refs[match.group(2)] = match.group(1).lower()
    return refs


def compare_refs(source: dict[str, str], destination: dict[str, str]) -> list[str]:
    diffs = []
    for name, sha in source.items():
        if name not in destination:
            diffs.append(f"Missing destination ref: {name}")
        elif sha != destination[name]:
            diffs.append(f"Hash mismatch for {name}: source={sha} destination={destination[name]}")
    for name in destination:
        if name not in source:
            diffs.append(f"Unexpected destination ref: {name}")
    return diffs


def compare_sets(expected: list[str], actual: list[str], value_name: str) -> list[str]:
    exp, act = set(expected), set(actual)
    return [f"Missing destination {value_name}: {v}" for v in sorted(exp - act)] + [
        f"Unexpected destination {value_name}: {v}" for v in sorted(act - exp)
    ]


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class Migration:
    def __init__(
        self,
        *,
        bitbucket_workspace: str,
        bitbucket_repo: str,
        github_org: str,
        github_repo: str,
        owner: str,
        uses_lfs: bool = False,
        working_directory: Path | str = "migration-work",
        minimum_free_space_gb: int = 5,
        assume_yes: bool = False,
        source_url: str | None = None,
        destination_url: str | None = None,
    ) -> None:
        self.workspace = bitbucket_workspace
        self.repo = bitbucket_repo
        self.org = github_org
        self.gh_repo = github_repo
        self.owner = owner
        self.uses_lfs = uses_lfs
        self.min_gb = minimum_free_space_gb
        self.assume_yes = assume_yes
        self.working_directory = Path(working_directory).resolve()
        self.source_url = source_url or f"https://bitbucket.org/{self.workspace}/{self.repo}.git"
        self.destination_url = destination_url or f"https://github.com/{self.org}/{self.gh_repo}.git"
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S%f")[:-3]
        self.repo_dir = self.working_directory / f"{self.workspace}--{self.repo}"
        self.run_dir = self.repo_dir / self.run_id
        self.log_path = self.run_dir / "migration.log"
        self.result_path = self.run_dir / "result.json"
        self.state_path = self.repo_dir / "migration-state.json"
        self.sign_off_path = self.repo_dir / "owner-sign-off.json"

    # ---- logging and process execution ----
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

    # ---- identity and state ----
    def identity(self) -> str:
        text = "|".join(
            [
                self.source_url.lower(),
                self.destination_url.lower(),
                self.owner.strip().lower(),
                str(bool(self.uses_lfs)).lower(),
            ]
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get_state(self) -> dict | None:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MigrationError(
                f"Migration state is unreadable and must be inspected manually: {self.state_path}"
            ) from exc

    def assert_state_identity(self, state: dict) -> None:
        if "migrationIdentity" in state:
            if state["migrationIdentity"] != self.identity():
                raise MigrationError(
                    "Migration state belongs to a different source, destination, owner, "
                    f"or LFS setting: {self.state_path}"
                )
            return
        legacy = (
            state.get("source") == self.source_url
            and state.get("destination") == self.destination_url
            and state.get("owner") == self.owner
            and bool(state.get("usesLfs")) == bool(self.uses_lfs)
        )
        if not legacy:
            raise MigrationError(
                f"Legacy migration state does not match the requested migration: {self.state_path}"
            )

    def assert_can_start(self) -> None:
        state = self.get_state()
        if state is None:
            return
        self.assert_state_identity(state)
        status = state.get("status")
        if status in PRE_PUSH_STATES:
            self.log("WARN", f"Retrying after pre-push state '{status}' from run '{state.get('runId')}'.")
            return
        if status in POST_PUSH_STATES:
            raise MigrationError(
                f"A prior migration reached post-push status '{status}' in run '{state.get('runId')}'. "
                f"Reconcile source and destination before any further push. State: {self.state_path}"
            )
        raise MigrationError(f"Migration state has unsupported status '{status}': {self.state_path}")

    def write_state(self, status: str, details: dict | None = None) -> None:
        previous = self.get_state()
        history: list = []
        created_at = now()
        verification_run_id = None
        if previous is not None:
            self.assert_state_identity(previous)
            history = list(previous.get("history", []))
            created_at = previous.get("createdAt", created_at)
            verification_run_id = previous.get("verificationRunId")
        if status == "Verified" and (previous is None or previous.get("status") != "Verified"):
            verification_run_id = self.run_id
        transition = {"runId": self.run_id, "status": status, "at": now()}
        if details:
            transition["details"] = details
        history.append(transition)
        data = {
            "schemaVersion": 2,
            "migrationIdentity": self.identity(),
            "runId": self.run_id,
            "status": status,
            "source": self.source_url,
            "destination": self.destination_url,
            "owner": self.owner,
            "usesLfs": bool(self.uses_lfs),
            "createdAt": created_at,
            "updatedAt": now(),
            "history": history,
        }
        if verification_run_id:
            data["verificationRunId"] = verification_run_id
        write_json(self.state_path, data)

    def write_recovery_event(self, event: str, approver: str, reference: str) -> None:
        state = self.get_state()
        if state is None or state.get("status") not in POST_PUSH_STATES:
            raise MigrationError("Recovery events require an existing post-push migration state.")
        self.write_state(
            state["status"], {"event": event, "approver": approver, "approvalReference": reference}
        )

    # ---- preflight checks ----
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
        if not metadata.get("private"):
            raise MigrationError("Destination GitHub repository must be private.")
        if not metadata.get("permissions", {}).get("push"):
            raise MigrationError(
                "Authenticated GitHub identity does not have push permission on the destination."
            )

    # ---- refs ----
    def remote_ref_lines(self, url: str, branches_and_tags_only: bool = False) -> list[str]:
        args = ["git", "ls-remote"]
        if branches_and_tags_only:
            args += ["--heads", "--tags"]
        return [line for line in self.run(args + [url]) if line]

    def comparable_refs(self, url: str, branches_and_tags_only: bool = False) -> dict[str, str]:
        refs = parse_ref_lines(self.remote_ref_lines(url, branches_and_tags_only))
        refs.pop("HEAD", None)
        return refs

    def reconciliation_result(self) -> dict:
        source = self.comparable_refs(self.source_url)
        destination = self.comparable_refs(self.destination_url)
        diffs = compare_refs(source, destination)

        def count(prefix: str) -> int:
            return sum(1 for d in diffs if d.startswith(prefix))

        return {
            "sourceRefs": source,
            "destinationRefs": destination,
            "differences": diffs,
            "exactMatch": not diffs,
            "sourceRefCount": len(source),
            "destinationRefCount": len(destination),
            "missingDestinationRefCount": count("Missing destination ref:"),
            "unexpectedDestinationRefCount": count("Unexpected destination ref:"),
            "hashMismatchCount": count("Hash mismatch for "),
        }

    @staticmethod
    def assert_snapshot_unchanged(expected: dict, actual: dict, description: str) -> None:
        if compare_refs(expected, actual):
            raise MigrationError(
                f"{description} changed after reconciliation. Run reconciliation again before retrying."
            )

    def lfs_object_ids(self, repository_path: Path, bare: bool = False) -> list[str]:
        prefix = [f"--git-dir={repository_path}"] if bare else ["-C", str(repository_path)]
        lines = self.run(["git", *prefix, "lfs", "ls-files", "--all", "--long"])
        ids: set[str] = set()
        for line in lines:
            if not line:
                continue
            match = re.match(r"^([0-9a-fA-F]{64})\s", line)
            if not match:
                raise MigrationError(f"Unexpected git lfs ls-files output: {line}")
            ids.add(match.group(1).lower())
        return sorted(ids)

    # ---- workflows ----
    def preflight(self) -> list[str]:
        self.log("INFO", "Starting preflight checks.")
        self.assert_command("git")
        self.run(["git", "--version"])
        if self.uses_lfs:
            self.run(["git", "lfs", "version"])
        self.assert_tls()
        self.assert_free_space()
        source_lines = self.remote_ref_lines(self.source_url, True)
        self.log("INFO", f"Source access confirmed; found {len(source_lines)} branch/tag ref entries.")
        self.assert_destination_metadata()
        destination_lines = self.remote_ref_lines(self.destination_url)
        if destination_lines:
            raise MigrationError(
                f"Destination repository is not empty; found {len(destination_lines)} ref entries."
            )
        self.log("INFO", "Destination is private, writable, and empty.")
        return source_lines

    def reconciliation_preflight(self) -> None:
        self.log("INFO", "Starting reconciliation access checks.")
        self.assert_command("git")
        self.run(["git", "--version"])
        self.assert_tls()
        self.remote_ref_lines(self.source_url)
        self.assert_destination_metadata()
        self.remote_ref_lines(self.destination_url)
        self.log("INFO", "Reconciliation access checks completed successfully.")

    def migrate_core(
        self,
        source_refs: dict[str, str],
        *,
        approved_retry: bool = False,
        skip_git_push: bool = False,
        expected_source: dict | None = None,
        expected_destination: dict | None = None,
        approver: str | None = None,
        reference: str | None = None,
    ) -> dict:
        mirror = self.run_dir / f"{self.repo}.git"
        verification = self.run_dir / "destination-verification"
        recovery = {"approvedRetry": True, "approver": approver, "approvalReference": reference}

        def checkpoint(status: str, event: str) -> None:
            if approved_retry:
                self.write_recovery_event(event, approver, reference)
            else:
                self.write_state(status)

        self.run(["git", "clone", "--mirror", self.source_url, str(mirror)])
        checkpoint("CloneCompleted", "RecoveryCloneCompleted")

        source_lfs: list[str] = []
        if self.uses_lfs:
            self.assert_free_space()
            self.run(["git", f"--git-dir={mirror}", "lfs", "fetch", "--all"])
            source_lfs = self.lfs_object_ids(mirror, bare=True)
            checkpoint("LfsFetched", "RecoveryLfsFetched")
            self.log("INFO", f"Fetched {len(source_lfs)} unique LFS objects from the source.")

        if approved_retry:
            self.assert_snapshot_unchanged(
                expected_source, self.comparable_refs(self.source_url), "Source refs"
            )
            self.assert_snapshot_unchanged(
                expected_destination, self.comparable_refs(self.destination_url), "Destination refs"
            )
        elif self.remote_ref_lines(self.destination_url):
            raise MigrationError("Destination gained refs after preflight. Mirror push has been blocked.")

        details = recovery if approved_retry else None
        if not skip_git_push:
            self.write_state("PushStarted", details)
            self.run(["git", f"--git-dir={mirror}", "push", "--mirror", self.destination_url])
            self.write_state("GitPushCompleted", details)
        else:
            self.log("INFO", "Git refs already match; mirror push is not required.")

        if self.uses_lfs:
            self.run(["git", f"--git-dir={mirror}", "lfs", "push", "--all", self.destination_url])
            self.write_state("LfsPushCompleted", details)

        destination_lfs: list[str] = []
        try:
            destination_refs = parse_ref_lines(self.remote_ref_lines(self.destination_url, True))
            diffs = compare_refs(source_refs, destination_refs)
            if diffs:
                for diff in diffs:
                    self.log("ERROR", diff)
                raise MigrationError(f"Source and destination refs differ in {len(diffs)} place(s).")
            self.log(
                "INFO",
                f"Verified {len(source_refs)} source branch/tag ref entries against the destination.",
            )
            if self.uses_lfs:
                self.run(
                    ["git", "clone", "--no-checkout", self.destination_url, str(verification)],
                    env={"GIT_LFS_SKIP_SMUDGE": "1"},
                )
                self.run(["git", "-C", str(verification), "lfs", "fetch", "--all"])
                self.run(["git", "-C", str(verification), "lfs", "fsck"])
                destination_lfs = self.lfs_object_ids(verification)
                lfs_diffs = compare_sets(source_lfs, destination_lfs, "LFS object")
                if lfs_diffs:
                    for diff in lfs_diffs:
                        self.log("ERROR", diff)
                    raise MigrationError(
                        f"Source and destination LFS object inventories differ in {len(lfs_diffs)} place(s)."
                    )
                self.log(
                    "INFO",
                    f"Verified {len(source_lfs)} unique LFS object IDs from the destination.",
                )
        except Exception as exc:
            self.write_state("VerificationFailed", {"error": to_safe_text(str(exc))})
            raise

        self.write_state("Verified")
        return {
            "sourceRefCount": len(source_refs),
            "destinationRefCount": len(destination_refs),
            "sourceLfsObjectCount": len(source_lfs),
            "destinationLfsObjectCount": len(destination_lfs),
        }

    def reconcile(self) -> dict:
        state = self.get_state()
        if state is None:
            raise MigrationError(f"No migration state exists to reconcile: {self.state_path}")
        self.assert_state_identity(state)
        if state["status"] not in POST_PUSH_STATES:
            raise MigrationError(
                f"Reconciliation is only required after push begins; current status is '{state['status']}'."
            )
        self.reconciliation_preflight()
        result = self.reconciliation_result()
        for diff in result["differences"]:
            self.log("WARN", diff)
        if result["exactMatch"]:
            self.log("INFO", "Source and destination refs match exactly.")
        self.write_state(
            state["status"],
            {
                "event": "Reconciled",
                "exactMatch": result["exactMatch"],
                "sourceRefCount": result["sourceRefCount"],
                "destinationRefCount": result["destinationRefCount"],
                "unexpectedDestinationRefCount": result["unexpectedDestinationRefCount"],
            },
        )
        return result

    def approved_retry(self, approver: str, reference: str) -> dict:
        self.assert_approval("Retry", approver, reference)
        state = self.get_state()
        if state is None:
            raise MigrationError(f"No migration state exists to retry: {self.state_path}")
        self.assert_state_identity(state)
        if state["status"] in ("Verified", "SignedOff"):
            raise MigrationError(f"Migration status '{state['status']}' cannot be retried.")
        if state["status"] not in POST_PUSH_STATES:
            raise MigrationError(f"Use normal migration retry for pre-push status '{state['status']}'.")

        self.reconciliation_preflight()
        self.assert_free_space()
        if self.uses_lfs:
            self.run(["git", "lfs", "version"])
        result = self.reconciliation_result()
        for diff in result["differences"]:
            self.log("WARN", diff)
        if result["unexpectedDestinationRefCount"] > 0:
            raise MigrationError(
                f"Retry is blocked because the destination has {result['unexpectedDestinationRefCount']} "
                "ref(s) absent from the source."
            )
        branch_tag_refs = self.comparable_refs(self.source_url, True)
        return self.migrate_core(
            branch_tag_refs,
            approved_retry=True,
            skip_git_push=result["exactMatch"],
            expected_source=result["sourceRefs"],
            expected_destination=result["destinationRefs"],
            approver=approver,
            reference=reference,
        )

    def sign_off(self, approver: str, reference: str, comments: str | None) -> dict:
        self.assert_approval("Sign-off", approver, reference)
        state = self.get_state()
        if state is None:
            raise MigrationError(f"No migration state exists to sign off: {self.state_path}")
        self.assert_state_identity(state)
        if state["status"] == "SignedOff":
            raise MigrationError(f"Migration was already signed off. Record: {self.sign_off_path}")
        if state["status"] != "Verified":
            raise MigrationError(
                f"Owner sign-off requires migration status 'Verified'; current status is '{state['status']}'."
            )
        record = {
            "schemaVersion": 1,
            "migrationIdentity": self.identity(),
            "source": self.source_url,
            "destination": self.destination_url,
            "owner": self.owner,
            "approver": approver,
            "approvalReference": reference,
            "status": "Approved",
            "comments": comments,
            "technicalStatus": state["status"],
            "technicalRunId": state.get("verificationRunId", state.get("runId")),
            "signedOffAt": now(),
        }
        write_json(self.sign_off_path, record)
        self.write_state(
            "SignedOff",
            {
                "event": "OwnerSignOff",
                "approver": approver,
                "approvalReference": reference,
                "signOffPath": str(self.sign_off_path),
            },
        )
        self.log("INFO", f"Owner sign-off recorded by {approver} with reference {reference}.")
        return record

    def confirm(self, description: str) -> bool:
        if self.assume_yes:
            return True
        if not sys.stdin.isatty():
            return False
        answer = input(f"About to {description}. Type 'yes' to continue: ")
        return answer.strip().lower() == "yes"

    def execute(self, operation: str, approver: str | None, reference: str | None, comments: str | None) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            for value, field in (
                (self.workspace, "bitbucket-workspace"),
                (self.repo, "bitbucket-repo"),
                (self.org, "github-org"),
                (self.gh_repo, "github-repo"),
            ):
                assert_safe_name(value, field)
            self.log("INFO", f"Run ID: {self.run_id}")
            self.log("INFO", f"Owner: {self.owner}")
            self.log("INFO", f"Source: {self.source_url}")
            self.log("INFO", f"Destination: {self.destination_url}")
            self.log("INFO", f"Uses LFS: {self.uses_lfs}")
            self.log("INFO", f"Operation: {operation}")

            if operation == "reconcile":
                result = self.reconcile()
                write_json(
                    self.result_path,
                    {
                        "runId": self.run_id,
                        "status": "Reconciled",
                        "source": self.source_url,
                        "destination": self.destination_url,
                        "priorMigrationStatus": self.get_state()["status"],
                        **{
                            k: result[k]
                            for k in (
                                "exactMatch",
                                "sourceRefCount",
                                "destinationRefCount",
                                "missingDestinationRefCount",
                                "unexpectedDestinationRefCount",
                                "hashMismatchCount",
                                "differences",
                            )
                        },
                        "completedAt": now(),
                    },
                )
                return

            if operation == "signoff":
                record = self.sign_off(approver, reference, comments)
                write_json(
                    self.result_path,
                    {
                        "runId": self.run_id,
                        "status": "SignedOff",
                        "source": self.source_url,
                        "destination": self.destination_url,
                        "owner": self.owner,
                        "approver": record["approver"],
                        "approvalReference": record["approvalReference"],
                        "comments": record["comments"],
                        "completedAt": record["signedOffAt"],
                    },
                )
                return

            if operation == "retry":
                self.assert_approval("Retry", approver, reference)
                if not self.confirm(
                    f"retry migration to {self.destination_url} using approval '{reference}'"
                ):
                    raise MigrationError("Approved retry was not confirmed.")
                verification = self.approved_retry(approver, reference)
            elif operation == "migrate":
                self.assert_can_start()
                self.write_state("NotStarted")
                source_lines = self.preflight()
                source_refs = parse_ref_lines(source_lines)
                self.log("INFO", "Preflight completed successfully.")
                if not self.confirm(f"mirror {self.source_url} to {self.destination_url}"):
                    raise MigrationError("Migration was not approved.")
                verification = self.migrate_core(source_refs)
            else:
                raise MigrationError(f"Unsupported operation '{operation}'.")

            self.log(
                "INFO",
                "Migration and verification completed successfully. Owner sign-off remains manual.",
            )
            is_retry = operation == "retry"
            write_json(
                self.result_path,
                {
                    "runId": self.run_id,
                    "status": "VerifiedPendingOwnerSignOff",
                    "owner": self.owner,
                    "source": self.source_url,
                    "destination": self.destination_url,
                    "usesLfs": bool(self.uses_lfs),
                    **verification,
                    "operation": operation,
                    "approver": approver if is_retry else None,
                    "approvalReference": reference if is_retry else None,
                    "completedAt": now(),
                },
            )
        except Exception as exc:
            self.log("ERROR", str(exc))
            write_json(
                self.result_path,
                {
                    "runId": self.run_id,
                    "status": "Failed",
                    "owner": self.owner,
                    "error": to_safe_text(str(exc)),
                    "failedAt": now(),
                },
            )
            raise


def name_arg(pattern: re.Pattern):
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise argparse.ArgumentTypeError(f"invalid value: {value}")
        return value

    return check


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Migrate a Bitbucket Cloud repository to GitHub.")
    p.add_argument("--bitbucket-workspace", required=True, type=name_arg(NAME_PATTERN))
    p.add_argument("--bitbucket-repo", required=True, type=name_arg(NAME_PATTERN))
    p.add_argument("--github-org", required=True, type=name_arg(ORG_PATTERN))
    p.add_argument("--github-repo", required=True, type=name_arg(NAME_PATTERN))
    p.add_argument("--owner", required=True)
    p.add_argument("--uses-lfs", action="store_true")
    p.add_argument("--working-directory", default=str(Path(__file__).resolve().parent / "migration-work"))
    p.add_argument("--minimum-free-space-gb", type=int, default=5, choices=range(1, 10241), metavar="1-10240")
    p.add_argument("--operation", choices=["migrate", "reconcile", "retry", "signoff"], default="migrate")
    p.add_argument("--approver")
    p.add_argument("--approval-reference")
    p.add_argument("--comments")
    p.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    migration = Migration(
        bitbucket_workspace=args.bitbucket_workspace,
        bitbucket_repo=args.bitbucket_repo,
        github_org=args.github_org,
        github_repo=args.github_repo,
        owner=args.owner,
        uses_lfs=args.uses_lfs,
        working_directory=args.working_directory,
        minimum_free_space_gb=args.minimum_free_space_gb,
        assume_yes=args.yes,
    )
    try:
        migration.execute(args.operation, args.approver, args.approval_reference, args.comments)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
