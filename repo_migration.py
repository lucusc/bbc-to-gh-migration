#!/usr/bin/env python3
"""Migrate a private Bitbucket Cloud repository to a private GitHub repository."""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from migration_execution import ExecutionMixin
from migration_git import GitMixin
from migration_preflight import PreflightMixin
from migration_state import StateMixin
from migration_utils import (
    MigrationError, assert_safe_name, compare_refs, compare_sets, parse_ref_lines,
    to_safe_text, write_json, now, NAME_PATTERN, ORG_PATTERN, PRE_PUSH_STATES, POST_PUSH_STATES,
)
from migration_workflow import WorkflowMixin


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
