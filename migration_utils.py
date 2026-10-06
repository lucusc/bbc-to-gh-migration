"""Shared migration constants, errors, and pure helper functions."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

PRE_PUSH_STATES = {"NotStarted", "CloneCompleted", "LfsFetched"}
POST_PUSH_STATES = {
    "PushStarted", "GitPushCompleted", "LfsPushCompleted", "VerificationFailed", "Verified", "SignedOff",
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
