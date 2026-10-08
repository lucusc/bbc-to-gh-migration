#!/usr/bin/env python3
"""Migrate a private Bitbucket Cloud repository to a GitHub repository."""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from .migration_batch import BatchEntry, load_batch
from .migration_dashboard import generate_dashboard
from .migration_execution import ExecutionMixin
from .migration_git import GitMixin
from .migration_preflight import PreflightMixin
from .migration_state import StateMixin
from .migration_utils import (
    MigrationError, assert_safe_name, compare_refs, compare_sets, parse_ref_lines,
    to_safe_text, write_json, now, NAME_PATTERN, ORG_PATTERN, PRE_PUSH_STATES, POST_PUSH_STATES,
)
from .migration_workflow import WorkflowMixin


class Migration(ExecutionMixin, StateMixin, PreflightMixin, GitMixin, WorkflowMixin):
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


def name_arg(pattern: re.Pattern):
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise argparse.ArgumentTypeError(f"invalid value: {value}")
        return value

    return check

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Migrate Bitbucket Cloud repositories to GitHub.")
    p.add_argument("--batch-file", type=Path, help="Reviewed JSON array of per-repository parameters.")
    p.add_argument("--bitbucket-workspace", required=True, type=name_arg(NAME_PATTERN), help="Source workspace for the repository or entire batch.")
    p.add_argument("--bitbucket-repo", type=name_arg(NAME_PATTERN))
    p.add_argument("--github-org", required=True, type=name_arg(ORG_PATTERN))
    p.add_argument("--github-repo", type=name_arg(NAME_PATTERN))
    p.add_argument("--owner")
    p.add_argument("--uses-lfs", action="store_true", default=None)
    p.add_argument("--working-directory", default=str(Path.cwd() / "migration-work"))
    p.add_argument("--minimum-free-space-gb", type=int, default=5, choices=range(1, 10241), metavar="1-10240")
    p.add_argument("--operation", choices=["migrate", "reconcile", "retry", "signoff"], default="migrate", help="Operation for the repository or entire batch (default: migrate).")
    p.add_argument("--approver")
    p.add_argument("--approval-reference")
    p.add_argument("--comments")
    p.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    return p


def run_batch(args: argparse.Namespace, entries: list[BatchEntry]) -> int:
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    summary_path = Path(args.working_directory).resolve() / "batch-runs" / batch_id / "summary.json"
    summary = {
        "batchId": batch_id,
        "manifest": str(args.batch_file.resolve()),
        "githubOrg": args.github_org,
        "bitbucketWorkspace": args.bitbucket_workspace,
        "operation": args.operation,
        "startedAt": now(),
        "status": "Running",
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "repositories": [],
    }
    write_json(summary_path, summary)
    for index, entry in enumerate(entries, 1):
        migration = Migration(
            bitbucket_workspace=entry.bitbucket_workspace,
            bitbucket_repo=entry.bitbucket_repo,
            github_org=entry.github_org,
            github_repo=entry.github_repo,
            owner=entry.owner,
            uses_lfs=entry.uses_lfs,
            working_directory=args.working_directory,
            minimum_free_space_gb=entry.minimum_free_space_gb,
            assume_yes=args.yes,
        )
        print(f"[{index}/{len(entries)}] {migration.source_url} -> {migration.destination_url}")
        result = {
            "source": migration.source_url,
            "destination": migration.destination_url,
            "owner": entry.owner,
            "usesLfs": entry.uses_lfs,
            "operation": entry.operation,
            "approvalStatus": "approved",
            "reviewer": entry.reviewer,
            "reviewedAt": entry.reviewed_at,
            "runId": migration.run_id,
            "resultPath": str(migration.result_path),
            "logPath": str(migration.log_path),
        }
        try:
            state = migration.get_state() if entry.operation == "reconcile" else None
            if entry.operation == "reconcile" and state is None:
                reason = "Repository has no migration state; reconciliation was not run."
                migration.log("WARN", reason)
                write_json(
                    migration.result_path,
                    {
                        "runId": migration.run_id,
                        "status": "NotApplicable",
                        "owner": entry.owner,
                        "source": migration.source_url,
                        "destination": migration.destination_url,
                        "usesLfs": entry.uses_lfs,
                        "operation": entry.operation,
                        "reason": reason,
                        "completedAt": now(),
                    },
                )
                generate_dashboard(migration.working_directory)
                result.update(status="Skipped", outcome="NotApplicable", reason=reason)
                summary["skipped"] += 1
                print(f"SKIPPED: {migration.workspace}/{migration.repo}: {reason}")
            else:
                migration.execute(
                    entry.operation,
                    entry.approver,
                    entry.approval_reference,
                    entry.comments,
                )
        except Exception as exc:
            result.update(status="Failed", error=to_safe_text(str(exc)))
            summary["failed"] += 1
            print(f"ERROR: {migration.workspace}/{migration.repo}: {to_safe_text(str(exc))}", file=sys.stderr)
        else:
            if result.get("status") != "Skipped":
                result["status"] = "Succeeded"
                summary["succeeded"] += 1
        summary["repositories"].append(result)
        write_json(summary_path, summary)
    summary.update(status="Failed" if summary["failed"] else "Succeeded", completedAt=now())
    write_json(summary_path, summary)
    print(
        f"Batch complete: {summary['succeeded']} succeeded, "
        f"{summary['skipped']} skipped, {summary['failed']} failed. Summary: {summary_path}"
    )
    return 1 if summary["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repository_flags = (
        "bitbucket_repo", "github_repo", "owner", "uses_lfs",
        "approver", "approval_reference", "comments",
    )
    if args.batch_file is not None:
        conflicting = [f"--{field.replace('_', '-')}" for field in repository_flags if getattr(args, field) is not None]
        if conflicting:
            parser.error(f"--batch-file cannot be combined with {', '.join(conflicting)}; put these parameters in the manifest.")
        try:
            entries = load_batch(
                args.batch_file, args.github_org, args.minimum_free_space_gb,
                bitbucket_workspace=args.bitbucket_workspace, operation=args.operation,
            )
            return run_batch(args, entries)
        except (MigrationError, OSError) as exc:
            print(f"ERROR: {to_safe_text(str(exc))}", file=sys.stderr)
            return 1
    missing = [
        f"--{field.replace('_', '-')}"
        for field in ("bitbucket_workspace", "bitbucket_repo", "github_repo", "owner")
        if getattr(args, field) is None
    ]
    if missing:
        parser.error(f"the following arguments are required without --batch-file: {', '.join(missing)}")
    migration = Migration(
        bitbucket_workspace=args.bitbucket_workspace,
        bitbucket_repo=args.bitbucket_repo,
        github_org=args.github_org,
        github_repo=args.github_repo,
        owner=args.owner,
        uses_lfs=bool(args.uses_lfs),
        working_directory=args.working_directory,
        minimum_free_space_gb=args.minimum_free_space_gb,
        assume_yes=args.yes,
    )
    try:
        migration.execute(args.operation or "migrate", args.approver, args.approval_reference, args.comments)
    except Exception as exc:
        print(f"ERROR: {to_safe_text(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
