"""Migration preflight, transfer, recovery, and sign-off workflows."""
from __future__ import annotations

import sys

from .migration_dashboard import generate_dashboard
from .migration_git import verified_ref_inventory
from .migration_utils import (
    MigrationError,
    POST_PUSH_STATES,
    assert_safe_name,
    compare_refs,
    compare_sets,
    now,
    parse_ref_lines,
    to_safe_text,
    write_json,
)


class WorkflowMixin:
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
        self.log("INFO", "Destination is writable and empty.")
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
            "verifiedRefs": verified_ref_inventory(destination_refs),
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
                        "owner": self.owner,
                        "source": self.source_url,
                        "destination": self.destination_url,
                        "usesLfs": bool(self.uses_lfs),
                        "operation": operation,
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
                        "verifiedRefs": (
                            verified_ref_inventory(result["destinationRefs"])
                            if result["exactMatch"]
                            else None
                        ),
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
                        "operation": operation,
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
                    "source": self.source_url,
                    "destination": self.destination_url,
                    "usesLfs": bool(self.uses_lfs),
                    "operation": operation,
                    "error": to_safe_text(str(exc)),
                    "failedAt": now(),
                },
            )
            raise
        finally:
            operation_failed = sys.exc_info()[0] is not None
            try:
                dashboard_path = generate_dashboard(self.working_directory)
                self.log("INFO", f"Migration dashboard updated: {dashboard_path}")
            except Exception as exc:
                self.log("ERROR", f"Could not update migration dashboard: {exc}")
                if not operation_failed:
                    raise
