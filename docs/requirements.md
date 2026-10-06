# Migration script requirements

## Purpose

Provide a PowerShell-based, auditable process for migrating approved private repositories from Bitbucket Cloud to private repositories in an explicitly specified GitHub organization while preserving Git history, branches, tags, and Git LFS objects.

The process must prioritize data integrity and deliberate operator control over unattended throughput. A mirror push can delete destination refs that do not exist at the source, so the script must not blindly repeat a push.

## Machine prerequisites

The migration machine must have:

- PowerShell 7.
- Git for Windows.
- Git LFS, verified with `git lfs version`.
- Git Credential Manager or another secure Git credential provider.
- Network access to Bitbucket Cloud, GitHub, and the GitHub API.
- Enough local disk for the bare mirror, all LFS objects, a verification clone, and transfer overhead.
- GitHub CLI only when the process is configured to create destination repositories.

TLS certificate verification must remain enabled. The process must fail if Git configuration or environment variables disable certificate verification.

## Authentication requirements

The operator must authenticate to:

- Bitbucket Cloud with permission to read each private source repository.
- GitHub with permission to create or write private repositories in the specified organization.

Credentials must be supplied through Git Credential Manager, GitHub CLI, SSH credential management, or environment-based authentication. Tokens and passwords must not appear in repository URLs, command arguments, logs, result files, or committed configuration.

## Migration input requirements

### Single-repository operation

The first delivery slice accepts:

- Bitbucket workspace.
- Bitbucket repository name.
- Approved GitHub organization.
- Approved GitHub repository name.
- Responsible owner.
- Whether the repository uses Git LFS.
- Local working directory.
- Minimum required free disk space.

The GitHub organization is a required, explicitly approved input.

### Reviewed batch operation

The later batch workflow must read a reviewed mapping containing:

- Bitbucket workspace.
- Bitbucket repository.
- Approved GitHub organization.
- Approved GitHub repository name.
- Responsible owner.
- Whether Git LFS is used.
- Approval status.
- Reviewer identity.
- Review timestamp.

The batch workflow must reject missing approvals, malformed values, duplicate sources, duplicate destinations, and destinations outside the explicitly approved GitHub organization.

## Destination requirements

Before migration, every destination repository must:

- Exist in the specified GitHub organization, unless explicitly created by a later repository-creation mode.
- Be private.
- Be writable by the authenticated identity.
- Be empty, with no branches, tags, or other Git refs.

The script must check emptiness during preflight and again immediately before `git push --mirror`. It must never overwrite a non-empty repository without explicit, repository-specific approval.

Destination repositories created by the process must be private and must not be initialized with a README, license, `.gitignore`, or other content.

## Preflight requirements

Before modifying a destination, the script must:

1. Validate required command availability and versions.
2. Validate TLS certificate verification.
3. Validate free disk space.
4. Confirm access to the private Bitbucket source.
5. Confirm GitHub destination existence, privacy, and push permission.
6. Confirm the destination has no refs.
7. Display the credential-free source-to-destination mapping.
8. Require confirmation unless the exact migration has already received explicit approval.

Any failed preflight check must stop that migration before a mirror push begins.

## Migration requirements

For each approved repository, the process must follow GitHub's documented mirror procedure:

```powershell
git clone --mirror <source> <mirror-path>
git --git-dir=<mirror-path> lfs fetch --all       # when LFS is used
git --git-dir=<mirror-path> push --mirror <destination>
git --git-dir=<mirror-path> lfs push --all <destination>  # when LFS is used
```

Every unsuccessful native command must cause the repository migration to fail. PowerShell must explicitly inspect native command exit codes rather than relying only on `$ErrorActionPreference`.

## Verification requirements

### Git refs

The script must independently query source and destination branch and tag refs and compare:

- Ref names.
- Branch commit hashes.
- Lightweight tag hashes.
- Annotated tag object hashes.
- Peeled annotated tag target hashes.

Missing, additional, or mismatched destination refs must fail verification. A repository is not technically successful until the comparison is exact.

### Git LFS

For repositories marked as using LFS, the process must:

- Fetch all source LFS objects.
- Inventory expected LFS object IDs.
- Push all LFS objects to GitHub.
- Fetch LFS content from the GitHub destination into a clean verification clone.
- Run `git lfs fsck`.
- Compare source and destination LFS object inventories.

A successful LFS push command alone is not sufficient verification.

## Logging and result requirements

The process must produce:

- A human-readable log for each run.
- A structured JSON result for each repository.
- A durable migration state for any repository whose mirror push has begun.

Records must include:

- Run identifier and timestamps.
- Credential-free source and destination URLs.
- Responsible owner.
- LFS status.
- Current migration state.
- Ref and LFS verification counts.
- Sanitized failure details.

Logs and results must redact credentials, authorization headers, and recognizable token formats.

## Failure and rerun requirements

The script must retain diagnostics and local staging data for failed repositories.

Rerun behavior must depend on the failure stage:

- Failures before `git push --mirror` may be retried after preflight.
- The process must write durable `PushStarted` state immediately before the mirror push.
- `PushStarted`, push failure, post-push interruption, or verification failure must block automatic mirror-push retries.
- A verified repository must not be pushed again by a normal rerun.
- Recovery after push begins must compare current source and destination refs and require explicit approval before another push.
- Destination-only refs must be reported and must never be automatically deleted during recovery.

## Owner sign-off requirements

Technical verification and owner acceptance are separate states.

The process must record:

- Owner or approver.
- Approval status.
- Approval timestamp.
- Ticket, change, or approval reference.
- Optional comments.

A technically verified repository remains pending until owner sign-off is explicitly recorded. Sign-off must not override failed technical verification.

## Security constraints

- Never embed credentials in URLs.
- Never write credentials or authorization headers to logs.
- Never disable TLS certificate verification.
- Never use `Invoke-Expression` for command construction.
- Pass native command arguments as discrete values.
- Reject unsafe repository names and path traversal.
- Require an explicitly approved destination organization and reject unsafe organization names.
- Fail closed when access, metadata, or verification cannot be determined.

## Phase 1 acceptance criteria

The single-repository slice is complete when:

- One approved private repository can be migrated to a pre-created empty private GitHub repository.
- Multiple branches and tags retain identical names and hashes.
- LFS repositories fetch, push, download, and verify their expected objects.
- Missing authentication, insufficient disk, disabled TLS verification, non-empty destinations, failed commands, and verification mismatches cause failure.
- A durable guard prevents an automatic repeat after mirror push begins.
- Logs and JSON results contain no credentials.
- Owner sign-off remains an explicit post-verification action.
