# Bitbucket Cloud to GitHub migration

`Invoke-RepoMigration.ps1` migrates one approved private Bitbucket Cloud repository to an empty private repository in the `bcgov-c` GitHub organization.

## Prerequisites

- PowerShell 7
- Git for Windows with Git Credential Manager
- Git LFS for repositories that use LFS
- Network access to Bitbucket Cloud, GitHub, and the GitHub API
- Credentials that can read the Bitbucket repository and push to the GitHub repository
- A pre-created private and empty destination repository
- Sufficient local disk for the mirror and all LFS objects

Authenticate using Git Credential Manager or GitHub CLI. Do not put credentials in repository URLs or script arguments. TLS certificate verification must remain enabled.

## Preflight usage

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

The current implementation performs preflight validation only. Migration execution and verification are added in the next delivery increment.
