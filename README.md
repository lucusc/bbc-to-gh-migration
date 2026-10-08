# Bitbucket Cloud to GitHub migration

`bbc_to_gh_migration` migrates one approved private Bitbucket Cloud repository to an empty repository in a specified GitHub organization. The destination may be public or private. It runs on Windows (Windows Terminal, cmd) and Linux (bash) and needs no PowerShell.

Detailed project documentation:

- [Migration requirements](docs/requirements.md)
- [Vertical delivery plan](docs/delivery-plan.md)

## Code structure

The project uses the standard Python `src` layout. The installable
`bbc_to_gh_migration` package is under `src/bbc_to_gh_migration`, with the
implementation split by responsibility:

- `cli.py` provides the command-line entry point and the public `Migration` API.
- `migration_utils.py` contains shared constants, validation, comparison, and JSON helpers.
- `migration_execution.py` contains sanitized logging and native command execution.
- `migration_state.py` contains durable state transitions and recovery identity checks.
- `migration_preflight.py` contains environment, TLS, disk, and GitHub metadata checks.
- `migration_git.py` contains Git ref and LFS inventory operations.
- `migration_workflow.py` contains migration, reconciliation, retry, and sign-off workflows.

## Prerequisites

- Python 3.10 or later (standard library only; no packages to install)
- Git with a credential helper (Git Credential Manager on Windows, or `gh auth setup-git` / another helper on Linux)
- Git LFS for repositories that use LFS
- Network access to Bitbucket Cloud, GitHub, and the GitHub API
- Credentials that can read the Bitbucket repository and push to the GitHub repository
- A pre-created empty destination repository (public or private)
- Sufficient local disk for the mirror and all LFS objects

Create a virtual environment and install the project:

```bash
python3 -m venv .venv
python3 -m pip install -e .
```

The editable install provides the `repo-migration` command. You can also run the
package as `python3 -m bbc_to_gh_migration`.

Authenticate using a Git credential helper or GitHub CLI. Do not put credentials in repository URLs or command arguments. TLS certificate verification must remain enabled.

On Windows use `python`; on Linux use `python3`. Examples below use bash line continuations (`\`); in Windows Terminal put the command on one line, or use the `^` (cmd) or backtick (PowerShell) continuation.

## Initial migration

```bash
repo-migration \
  --bitbucket-workspace workspace \
  --bitbucket-repo source-repo \
  --github-org organization \
  --github-repo destination-repo \
  --owner responsible-owner \
  --uses-lfs \
  --working-directory /data/repo-migrations \
  --minimum-free-space-gb 20
```

The tool:

1. Validates tools, TLS settings, disk capacity, source access, and GitHub permissions.
2. Confirms that the destination is writable and empty.
3. Requests confirmation before the first mirror push.
4. Creates a mirror clone and fetches all LFS objects when requested.
5. Pushes all Git refs and LFS objects.
6. Compares all source and destination branch and tag hashes.
7. For LFS repositories, downloads from GitHub and compares the LFS object inventory.
8. Writes a sanitized log and JSON result under the working directory.
9. Refreshes a static HTML dashboard at `migration-dashboard.html` in the working directory.

Open `migration-dashboard.html` directly in a browser to see the latest recorded
outcome for every repository in that working directory. The page is self-contained
and needs no web server or network access. It includes summary counts, repository
and status filters, verified branch, tag, other-ref and LFS details with hashes,
run and approval metadata, and relative links to each run's `result.json` and
`migration.log`. Sign-off entries retain the details from their technical verification.
The dashboard is refreshed after successful and failed migrate,
reconcile, retry, and sign-off operations, so it is ready for future multi-repository
runs that share the same working directory.

The tool writes `migration-state.json` immediately before the mirror push. Any later migrate run for the same source repository is blocked, including after a successful migration. Reconcile the source and destination before removing or changing this guard; a repeated mirror push can delete destination-only refs.

The confirmation is an interactive `yes` prompt. When stdin is not a terminal the run is refused unless `--yes` is given. Use `--yes` only when the complete source and destination mapping has already received explicit approval.

The exit code is 0 on success and 1 on failure.

## Reconciliation

After a push starts, normal migration runs are blocked. Compare the current source and destination without modifying either repository:

```bash
repo-migration \
  --bitbucket-workspace workspace --bitbucket-repo source-repo \
  --github-org organization --github-repo destination-repo \
  --owner responsible-owner --working-directory /data/repo-migrations \
  --operation reconcile
```

The result lists missing, additional, and mismatched refs. Destination-only refs block automated recovery because a repeated mirror push would delete them.

## Approved retry

Retry after push begins only with repository-specific approval:

```bash
repo-migration \
  --bitbucket-workspace workspace --bitbucket-repo source-repo \
  --github-org organization --github-repo destination-repo \
  --owner responsible-owner --working-directory /data/repo-migrations \
  --operation retry --approver approver-name --approval-reference CHANGE-1234
```

The tool reconciles again, blocks destination-only refs, and confirms that source and destination snapshots have not changed before pushing. If refs already match, the Git mirror push is skipped and verification continues. LFS objects are still pushed when the repository uses LFS.

## Owner sign-off

After technical verification:

```bash
repo-migration \
  --bitbucket-workspace workspace --bitbucket-repo source-repo \
  --github-org organization --github-repo destination-repo \
  --owner responsible-owner --working-directory /data/repo-migrations \
  --operation signoff --approver repository-owner \
  --approval-reference CHANGE-1234 --comments "Migration accepted."
```

Sign-off is rejected unless the durable migration state is `Verified`. The approval is written to `owner-sign-off.json`, and the migration state advances to `SignedOff`.

## Tests

```bash
python3 -m pip install -e ".[test]"
python3 -m pytest
```

CI runs the suite on Windows and Linux (`.github/workflows/tests.yml`).

## Live Bitbucket test data

`tests/seed_bitbucket_repositories.py` creates and seeds three private,
disposable Bitbucket Cloud repositories:

- `migration-test-standard` has branches and tags but no LFS content or secrets.
- `migration-test-lfs` has two historical versions of a deterministic LFS object.
- `migration-test-secret-blocked` has only nonfunctional AWS example credentials
  and a custom secret marker for exercising GitHub push protection.

The script creates repositories but never deletes or overwrites existing ones.
It uses the Bitbucket REST API for creation and the configured Git credential
helper for Git pushes. Set `BITBUCKET_API_TOKEN`; also set
`BITBUCKET_API_USER` when the token requires HTTP Basic authentication. For a
Bearer workspace or project access token, leave `BITBUCKET_API_USER` unset.
Do not put credentials in repository URLs or command arguments.

The Bitbucket project key can differ from its display name. Find the key in the
project settings, then run:

```powershell
$env:BITBUCKET_API_TOKEN = "<token>"
$env:BITBUCKET_API_USER = "<account-email-or-username>"
python tests\seed_bitbucket_repositories.py --workspace lwc-migrate --project-key "<project-key>"
```

The authenticated Git credential helper must be able to push to the
`lwc-migrate` workspace, and `git-lfs` must be installed. To make the custom
marker deterministic for GitHub Secret Protection, configure this custom
pattern with push protection:

```text
LUCUS_LABS_MIGRATION_TEST_SECRET_[A-Z0-9]{32}
```

The repository also contains AWS's documented nonfunctional example key pair
for testing built-in secret patterns. Neither value can authenticate to a
service.
