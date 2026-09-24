# Bitbucket Cloud to GitHub migration

`Invoke-RepoMigration.ps1` migrates one approved private Bitbucket Cloud repository to an empty private repository in the `bcgov-c` GitHub organization.

Detailed project documentation:

- [Migration requirements](docs/requirements.md)
- [Vertical delivery plan](docs/delivery-plan.md)

## Prerequisites

- PowerShell 7
- Git for Windows with Git Credential Manager
- Git LFS for repositories that use LFS
- Network access to Bitbucket Cloud, GitHub, and the GitHub API
- Credentials that can read the Bitbucket repository and push to the GitHub repository
- A pre-created private and empty destination repository
- Sufficient local disk for the mirror and all LFS objects

Authenticate using Git Credential Manager or GitHub CLI. Do not put credentials in repository URLs or script arguments. TLS certificate verification must remain enabled.

## Usage

```powershell
.\Invoke-RepoMigration.ps1 `
  -BitbucketWorkspace "workspace" `
  -BitbucketRepo "source-repo" `
  -GitHubRepo "destination-repo" `
  -Owner "responsible-owner" `
  -UsesLfs `
  -WorkingDirectory "D:\repo-migrations" `
  -MinimumFreeSpaceGB 20
```

The script:

1. Validates tools, TLS settings, disk capacity, source access, and GitHub permissions.
2. Confirms that the destination is private and empty.
3. Requests confirmation before the first mirror push.
4. Creates a mirror clone and fetches all LFS objects when requested.
5. Pushes all Git refs and LFS objects.
6. Compares all source and destination branch and tag hashes.
7. For LFS repositories, downloads from GitHub and compares the LFS object inventory.
8. Writes a sanitized log and JSON result under the working directory.

The script writes `migration-state.json` immediately before the mirror push. Any later run for the same source repository is blocked, including after a successful migration. Reconcile the source and destination manually before removing or changing this guard; a repeated mirror push can delete destination-only refs.

Use `-Confirm:$false` only when the complete source and destination mapping has already received explicit approval.

## Tests

```powershell
Invoke-Pester .\tests
```
