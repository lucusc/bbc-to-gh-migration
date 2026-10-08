"""Validation of reviewed, per-repository batch parameters."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .migration_utils import MigrationError, NAME_PATTERN, ORG_PATTERN, assert_safe_name


@dataclass(frozen=True)
class BatchEntry:
    bitbucket_workspace: str
    bitbucket_repo: str
    github_org: str
    github_repo: str
    owner: str
    uses_lfs: bool
    minimum_free_space_gb: int
    operation: str
    approver: str | None
    approval_reference: str | None
    comments: str | None
    reviewer: str
    reviewed_at: str


def load_batch(
    path: Path, github_org: str, minimum_free_space_gb: int, *,
    bitbucket_workspace: str, operation: str = "migrate",
) -> list[BatchEntry]:
    for field, value, pattern in (
        ("bitbucket_workspace", bitbucket_workspace, NAME_PATTERN),
        ("github_org", github_org, ORG_PATTERN),
    ):
        if not pattern.fullmatch(value):
            raise MigrationError(f"Invalid batch {field}.")
        assert_safe_name(value, f"Batch {field}")
    if operation not in ("migrate", "reconcile", "retry", "signoff"):
        raise MigrationError("Unsupported batch operation.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError(f"Cannot read batch manifest '{path}': {exc}") from exc
    if not isinstance(data, list) or not data:
        raise MigrationError("Batch manifest must be a non-empty JSON array of repository parameters.")

    required = {
        "bitbucket_repo", "github_repo", "owner",
        "uses_lfs", "approval_status", "reviewer", "reviewed_at",
    }
    optional = {
        "bitbucket_workspace", "github_org", "minimum_free_space_gb", "operation",
        "approver", "approval_reference", "comments",
    }
    entries = []
    sources: set[str] = set()
    destinations: set[tuple[str, str]] = set()
    for index, item in enumerate(data, 1):
        label = f"Batch entry {index}"
        if not isinstance(item, dict):
            raise MigrationError(f"{label} must be a JSON object.")
        missing = required - item.keys()
        unknown = item.keys() - required - optional
        if missing:
            raise MigrationError(f"{label} is missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise MigrationError(f"{label} has unsupported fields: {', '.join(sorted(unknown))}")
        for field in required - {"uses_lfs"}:
            if not isinstance(item[field], str) or not item[field].strip():
                raise MigrationError(f"{label}: {field} must be a non-empty string.")
        for field in ("bitbucket_workspace", "bitbucket_repo", "github_org", "github_repo"):
            if field not in item:
                continue
            value = item[field]
            if not isinstance(value, str) or not value.strip():
                raise MigrationError(f"{label}: {field} must be a non-empty string.")
            pattern = ORG_PATTERN if field == "github_org" else NAME_PATTERN
            if not pattern.fullmatch(value):
                raise MigrationError(f"{label}: invalid {field}.")
            assert_safe_name(value, f"{label}: {field}")
        if item.get("github_org", github_org).casefold() != github_org.casefold():
            raise MigrationError(f"{label}: destination is outside the approved GitHub organization.")
        if item.get("bitbucket_workspace", bitbucket_workspace).casefold() != bitbucket_workspace.casefold():
            raise MigrationError(f"{label}: source is outside the batch Bitbucket workspace.")
        if item["approval_status"] != "approved":
            raise MigrationError(f"{label}: approval_status must be 'approved'.")
        try:
            reviewed_at = datetime.fromisoformat(item["reviewed_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise MigrationError(f"{label}: reviewed_at must be an ISO 8601 timestamp.") from exc
        if reviewed_at.tzinfo is None:
            raise MigrationError(f"{label}: reviewed_at must include a timezone.")
        if not isinstance(item["uses_lfs"], bool):
            raise MigrationError(f"{label}: uses_lfs must be a JSON boolean.")
        min_gb = item.get("minimum_free_space_gb", minimum_free_space_gb)
        if type(min_gb) is not int or not 1 <= min_gb <= 10240:
            raise MigrationError(f"{label}: minimum_free_space_gb must be an integer from 1 to 10240.")
        entry_operation = item.get("operation", operation)
        if not isinstance(entry_operation, str) or entry_operation not in ("migrate", "reconcile", "retry", "signoff"):
            raise MigrationError(f"{label}: unsupported operation.")
        if entry_operation != operation:
            raise MigrationError(f"{label}: operation must match the batch --operation '{operation}'.")
        for field in ("approver", "approval_reference", "comments"):
            value = item.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise MigrationError(f"{label}: {field} must be a non-empty string when supplied.")
        if operation in ("retry", "signoff"):
            for field in ("approver", "approval_reference"):
                if not item.get(field):
                    raise MigrationError(f"{label}: {operation} requires {field}.")

        # Match the existing on-disk state key, including case-insensitive filesystems.
        source = f"{bitbucket_workspace}--{item['bitbucket_repo']}".casefold()
        destination = (github_org.casefold(), item["github_repo"].casefold())
        if source in sources:
            raise MigrationError(f"{label}: duplicate source or conflicting repository state directory.")
        if destination in destinations:
            raise MigrationError(f"{label}: duplicate destination.")
        sources.add(source)
        destinations.add(destination)
        entries.append(BatchEntry(
            bitbucket_workspace=bitbucket_workspace,
            bitbucket_repo=item["bitbucket_repo"],
            github_org=github_org,
            github_repo=item["github_repo"],
            owner=item["owner"],
            uses_lfs=item["uses_lfs"],
            minimum_free_space_gb=min_gb,
            operation=operation,
            approver=item.get("approver"),
            approval_reference=item.get("approval_reference"),
            comments=item.get("comments"),
            reviewer=item["reviewer"],
            reviewed_at=item["reviewed_at"],
        ))
    return entries
