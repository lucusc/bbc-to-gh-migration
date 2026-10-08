"""Durable migration state and approval records."""
from __future__ import annotations

import json
import hashlib

from .migration_utils import MigrationError, POST_PUSH_STATES, PRE_PUSH_STATES, now, write_json


class StateMixin:
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
