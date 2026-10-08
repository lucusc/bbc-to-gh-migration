"""Git refs, LFS inventory, and reconciliation operations."""
from __future__ import annotations

import re
from pathlib import Path

from .migration_utils import MigrationError, compare_refs, parse_ref_lines


class GitMixin:
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
