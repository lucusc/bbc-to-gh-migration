# Vertical delivery plan

## Delivery strategy

Deliver complete, usable vertical slices. The first slice migrates one repository safely as soon as possible. Later slices extend that proven operation rather than delaying it behind batch orchestration or repository provisioning.

## Phase 1: Migrate one repository

**Status:** Implemented.

**Outcome:** An operator can migrate one approved private Bitbucket Cloud repository to a pre-created, empty private repository in an explicitly specified GitHub organization.

**Delivered capabilities:**

- Parameters for workspace, source repository, GitHub organization, destination repository, owner, LFS use, working directory, and disk threshold.
- Tool, TLS, disk, authentication, destination metadata, permission, and emptiness preflight.
- Explicit confirmation before migration.
- Mirror clone and mirror push.
- Optional LFS fetch and push.
- Source and destination branch/tag/hash comparison.
- Destination-backed LFS download, `git lfs fsck`, and object inventory comparison.
- Credential-redacted human-readable logs and JSON results.
- Durable push-state guard that blocks blind reruns.
- Focused unit tests and a local end-to-end non-LFS mirror test.

**Operational limitation:** Destination repositories must be created beforehand. Recovery after a push starts is intentionally blocked and requires manual reconciliation. Owner sign-off is manual.

## Phase 2: Harden single-repository recovery

**Status:** In progress. State history, reconciliation, approved retry, ref-deletion protection, and owner sign-off are implemented. A complete local Git LFS integration test remains.

**Outcome:** Operators can diagnose and safely recover interrupted or partially completed migrations without manually editing state.

**Remaining work:**

- Add a local Git LFS end-to-end integration test that exercises source object fetch, destination push, clean destination download, `git lfs fsck`, and source/destination object inventory comparison.
- Run the full Pester suite with Git LFS available and confirm the new integration test passes without skips.
- Mark Phase 2 as implemented after the Git LFS integration test passes.

**Scope:**

- Expand durable state transitions:
  - `NotStarted`
  - `CloneCompleted`
  - `LfsFetched`
  - `PushStarted`
  - `GitPushCompleted`
  - `LfsPushCompleted`
  - `VerificationFailed`
  - `Verified`
  - `SignedOff`
- Add a reconciliation command that performs read-only source and destination comparisons.
- Distinguish failures that occurred before and after mirror push.
- Permit automatic retry only for pre-push failures.
- Require repository-specific approval and a change reference before any post-push retry.
- Detect destination refs added after the first migration attempt.
- Add an explicit owner sign-off command and record.
- Add a complete local Git LFS integration test.

**Exit criteria:**

- Interruptions before push can resume safely.
- Interruptions during or after push cannot trigger an automatic mirror push.
- Reconciliation clearly reports missing, additional, and mismatched refs.
- A verified repository cannot be migrated again through the normal command.
- Owner sign-off is persisted separately from technical verification.

## Phase 3: Add the reviewed batch manifest

**Outcome:** A reviewed set of repositories can be processed using the proven single-repository operation.

**Scope:**

- Add CSV or JSON manifest parsing.
- Validate required mapping and approval fields.
- Reject duplicate or conflicting mappings.
- Add batch-wide `-PreflightOnly`.
- Add selection of one repository from the manifest.
- Preflight the complete batch before modifying any destination.
- Use isolated staging, logs, results, and state for every repository.
- Continue to later repositories after a repository-level failure while returning an overall failed status.
- Generate a batch summary covering successful, failed, blocked, and pending-sign-off repositories.

**Exit criteria:**

- Invalid or unapproved mappings fail before cloning.
- Every repository retains independent diagnostics and state.
- One failure does not corrupt or conceal another repository's result.
- Batch output identifies exactly which repositories require operator action.

## Phase 4: Create destination repositories

**Outcome:** The script can optionally create approved private destination repositories.

**Scope:**

- Add an explicit destination-creation mode using GitHub CLI.
- Validate `gh` availability and authenticated organization access.
- Create repositories only in the explicitly approved GitHub organization.
- Create repositories as private and uninitialized.
- Confirm repository privacy and push permission after creation.
- Never recreate or replace an existing repository.
- Preserve the immediate pre-push emptiness check.

**Exit criteria:**

- Only approved manifest entries can create repositories.
- Created destinations are private and contain no refs.
- Insufficient permissions stop processing before migration.
- Existing destinations are inspected rather than overwritten.

## Phase 5: Production hardening and pilot

**Outcome:** The process is ready for controlled production batches.

**Scope:**

- Expand Pester coverage for command failures, state transitions, reconciliation, manifest validation, sign-off, and sanitization.
- Test annotated and lightweight tags, empty sources, unusual valid names, LFS failures, insufficient disk, and interrupted pushes.
- Add operator procedures for authentication, preflight, migration, reconciliation, sign-off, and cleanup.
- Add controlled cleanup that removes only explicitly selected successful staging directories after sign-off.
- Run a pilot using representative non-LFS and LFS repositories.
- Review measured disk use, transfer duration, failure behavior, and operator workflow.

**Release gate:**

- Pilot repositories pass independent Git and LFS verification.
- Recovery scenarios have been exercised end to end.
- Owners complete sign-off.
- Operators can determine whether a repository is safe to retry without inspecting or modifying the script.

## Recommended implementation order

1. Run a controlled Phase 1 pilot on one non-LFS repository.
2. Run a controlled Phase 1 pilot on one LFS repository.
3. Implement Phase 2 before attempting migrations where rerun recovery is operationally important.
4. Add the Phase 3 manifest only after the single-repository workflow and recovery procedure are proven.
5. Add automated destination creation after repository governance and naming approval are settled.
6. Complete the production hardening gate before broad migration.
