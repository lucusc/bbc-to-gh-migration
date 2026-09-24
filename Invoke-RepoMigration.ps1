[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$BitbucketWorkspace,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$BitbucketRepo,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$GitHubRepo,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$Owner,

    [switch]$UsesLfs,

    [ValidateNotNullOrEmpty()]
    [string]$WorkingDirectory = (Join-Path $PSScriptRoot 'migration-work'),

    [ValidateRange(1, 10240)]
    [int]$MinimumFreeSpaceGB = 5,

    [ValidateSet('Migrate', 'Reconcile', 'Retry', 'SignOff')]
    [string]$Operation = 'Migrate',

    [string]$Approver,

    [string]$ApprovalReference,

    [string]$Comments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:GitHubOrganization = 'bcgov-c'
$script:RunId = Get-Date -Format 'yyyyMMdd-HHmmssfff'
$script:RepositoryDirectory = Join-Path $WorkingDirectory "$BitbucketWorkspace--$BitbucketRepo"
$script:RunDirectory = Join-Path $script:RepositoryDirectory $script:RunId
$script:LogPath = Join-Path $script:RunDirectory 'migration.log'
$script:ResultPath = Join-Path $script:RunDirectory 'result.json'
$script:StatePath = Join-Path $script:RepositoryDirectory 'migration-state.json'
$script:SignOffPath = Join-Path $script:RepositoryDirectory 'owner-sign-off.json'

function ConvertTo-SafeText {
    param([AllowNull()][string]$Text)

    if ($null -eq $Text) {
        return ''
    }

    $safeText = $Text -replace '(?i)(https?://)[^/@\s]+@', '$1'
    $safeText = $safeText -replace '(?i)(authorization\s*:\s*)(basic|bearer)\s+\S+', '$1[REDACTED]'
    $safeText = $safeText -replace '(?i)\b(ghp|github_pat|bbp)_[A-Za-z0-9_]+\b', '[REDACTED]'
    return $safeText
}

function Write-MigrationLog {
    param(
        [Parameter(Mandatory)]
        [ValidateSet('INFO', 'WARN', 'ERROR')]
        [string]$Level,

        [Parameter(Mandatory)]
        [string]$Message
    )

    $entry = '{0:o} [{1}] {2}' -f (Get-Date), $Level, (ConvertTo-SafeText $Message)
    $logDirectory = Split-Path $script:LogPath -Parent
    if (-not (Test-Path -LiteralPath $logDirectory)) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    }
    Write-Information $entry -InformationAction Continue
    Add-Content -LiteralPath $script:LogPath -Value $entry -Encoding utf8
}

function Format-NativeArgument {
    param([Parameter(Mandatory)][string]$Argument)

    $safeArgument = ConvertTo-SafeText $Argument
    if ($safeArgument -match '[\s"]') {
        return '"{0}"' -f ($safeArgument -replace '"', '\"')
    }

    return $safeArgument
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,

        [Parameter()]
        [string[]]$ArgumentList = @(),

        [switch]$CaptureOutput
    )

    $displayArguments = ($ArgumentList | ForEach-Object { Format-NativeArgument $_ }) -join ' '
    Write-MigrationLog INFO "Running: $FilePath $displayArguments"

    $output = & $FilePath @ArgumentList 2>&1
    $exitCode = $LASTEXITCODE
    $safeOutput = @($output | ForEach-Object { ConvertTo-SafeText "$_" })

    foreach ($line in $safeOutput) {
        if ($line) {
            Write-MigrationLog INFO $line
        }
    }

    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $FilePath $displayArguments"
    }

    if ($CaptureOutput) {
        return $safeOutput
    }
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$Data,
        [Parameter(Mandatory)][string]$Path
    )

    $Data | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Path -Encoding utf8
}

function Get-MigrationIdentity {
    param(
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl
    )

    $identityText = '{0}|{1}|{2}|{3}' -f
        $SourceUrl.ToLowerInvariant(),
        $DestinationUrl.ToLowerInvariant(),
        $Owner.Trim().ToLowerInvariant(),
        ([bool]$UsesLfs).ToString().ToLowerInvariant()
    $bytes = [Text.Encoding]::UTF8.GetBytes($identityText)
    $hash = [Security.Cryptography.SHA256]::HashData($bytes)
    return [Convert]::ToHexString($hash).ToLowerInvariant()
}

function Get-MigrationState {
    if (-not (Test-Path -LiteralPath $script:StatePath)) {
        return $null
    }

    try {
        return Get-Content -LiteralPath $script:StatePath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Migration state is unreadable and must be inspected manually: $script:StatePath"
    }
}

function Assert-StateIdentity {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl
    )

    $expectedIdentity = Get-MigrationIdentity $SourceUrl $DestinationUrl
    if ($State.PSObject.Properties.Name -contains 'migrationIdentity') {
        if ($State.migrationIdentity -ne $expectedIdentity) {
            throw "Migration state belongs to a different source, destination, owner, or LFS setting: $script:StatePath"
        }
        return
    }

    $legacyMatches = $State.source -eq $SourceUrl -and
        $State.destination -eq $DestinationUrl -and
        $State.owner -eq $Owner -and
        [bool]$State.usesLfs -eq [bool]$UsesLfs
    if (-not $legacyMatches) {
        throw "Legacy migration state does not match the requested migration: $script:StatePath"
    }
}

function Test-IsPrePushState {
    param([Parameter(Mandatory)][string]$Status)

    return $Status -in @('NotStarted', 'CloneCompleted', 'LfsFetched')
}

function Test-IsPostPushState {
    param([Parameter(Mandatory)][string]$Status)

    return $Status -in @(
        'PushStarted',
        'GitPushCompleted',
        'LfsPushCompleted',
        'VerificationFailed',
        'Verified',
        'SignedOff'
    )
}

function Assert-RecoveryApproval {
    if ([string]::IsNullOrWhiteSpace($Approver)) {
        throw 'Retry requires -Approver.'
    }
    if ([string]::IsNullOrWhiteSpace($ApprovalReference)) {
        throw 'Retry requires -ApprovalReference.'
    }
}

function Assert-SafeRepositoryName {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$FieldName
    )

    if ($Name -notmatch '^[A-Za-z0-9._-]+$' -or $Name.StartsWith('.') -or $Name.EndsWith('.git')) {
        throw "$FieldName contains unsupported characters or formatting: $Name"
    }
}

function Assert-CommandAvailable {
    param([Parameter(Mandatory)][string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command is not available on PATH: $Name"
    }
}

function Assert-TlsVerification {
    $configEntries = @(& git config --list --show-origin 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to inspect Git configuration for TLS certificate verification.'
    }

    foreach ($entry in $configEntries) {
        if ($entry -match '(?i)\shttp(?:\..+)?\.sslverify=false\s*$') {
            throw "Git TLS certificate verification is disabled: $entry"
        }
    }

    if ($env:GIT_SSL_NO_VERIFY -and $env:GIT_SSL_NO_VERIFY -notin @('0', 'false', 'False', 'FALSE')) {
        throw 'GIT_SSL_NO_VERIFY disables TLS certificate verification.'
    }
}

function Assert-FreeDiskSpace {
    $resolvedWorkingDirectory = [System.IO.Path]::GetFullPath($WorkingDirectory)
    $root = [System.IO.Path]::GetPathRoot($resolvedWorkingDirectory)
    $drive = [System.IO.DriveInfo]::new($root)
    $requiredBytes = $MinimumFreeSpaceGB * 1GB

    if ($drive.AvailableFreeSpace -lt $requiredBytes) {
        throw ('Insufficient free space on {0}. Available: {1:N2} GB; required minimum: {2} GB.' -f
            $root, ($drive.AvailableFreeSpace / 1GB), $MinimumFreeSpaceGB)
    }

    Write-MigrationLog INFO ('Disk space on {0}: {1:N2} GB available.' -f $root, ($drive.AvailableFreeSpace / 1GB))
}

function Get-GitHubRepositoryMetadata {
    param([Parameter(Mandatory)][string]$Repository)

    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if ($gh) {
        $json = & gh api "repos/$script:GitHubOrganization/$Repository" 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to read GitHub repository metadata for $script:GitHubOrganization/$Repository."
        }

        return ($json | ConvertFrom-Json)
    }

    $credentialInput = "protocol=https`nhost=github.com`n`n"
    $credentialOutput = $credentialInput | & git credential fill 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to retrieve GitHub credentials from Git Credential Manager.'
    }

    $credential = @{}
    foreach ($line in $credentialOutput) {
        if ($line -match '^([^=]+)=(.*)$') {
            $credential[$Matches[1]] = $Matches[2]
        }
    }

    if (-not $credential.username -or -not $credential.password) {
        throw 'Git Credential Manager did not return a GitHub username and credential.'
    }

    $basicValue = [Convert]::ToBase64String(
        [Text.Encoding]::ASCII.GetBytes("$($credential.username):$($credential.password)")
    )

    try {
        return Invoke-RestMethod `
            -Uri "https://api.github.com/repos/$script:GitHubOrganization/$Repository" `
            -Headers @{
                Accept = 'application/vnd.github+json'
                Authorization = "Basic $basicValue"
                'X-GitHub-Api-Version' = '2022-11-28'
            }
    }
    catch {
        throw "Unable to read GitHub repository metadata for $script:GitHubOrganization/$Repository."
    }
    finally {
        $basicValue = $null
        $credential.Clear()
        $credentialOutput = $null
    }
}

function Get-RemoteRefLines {
    param(
        [Parameter(Mandatory)][string]$RemoteUrl,
        [switch]$BranchesAndTagsOnly
    )

    $arguments = @('ls-remote')
    if ($BranchesAndTagsOnly) {
        $arguments += @('--heads', '--tags')
    }
    $arguments += $RemoteUrl

    return @(Invoke-NativeCommand git $arguments -CaptureOutput)
}

function ConvertTo-RefMap {
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Lines)

    $refs = [ordered]@{}
    foreach ($line in $Lines) {
        if ($line -notmatch '^([0-9a-fA-F]{40,64})\s+(.+)$') {
            throw "Unexpected git ls-remote output: $line"
        }

        $refs[$Matches[2]] = $Matches[1].ToLowerInvariant()
    }

    return $refs
}

function Get-ComparableRemoteRefMap {
    param(
        [Parameter(Mandatory)][string]$RemoteUrl,
        [switch]$BranchesAndTagsOnly
    )

    $lines = @(Get-RemoteRefLines $RemoteUrl -BranchesAndTagsOnly:$BranchesAndTagsOnly)
    $refs = ConvertTo-RefMap $lines
    $refs.Remove('HEAD')
    return $refs
}

function Compare-RefMaps {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$SourceRefs,
        [Parameter(Mandatory)][System.Collections.IDictionary]$DestinationRefs
    )

    $differences = [System.Collections.Generic.List[string]]::new()

    foreach ($refName in $SourceRefs.Keys) {
        if (-not $DestinationRefs.Contains($refName)) {
            $differences.Add("Missing destination ref: $refName")
        }
        elseif ($SourceRefs[$refName] -ne $DestinationRefs[$refName]) {
            $differences.Add(
                "Hash mismatch for ${refName}: source=$($SourceRefs[$refName]) destination=$($DestinationRefs[$refName])"
            )
        }
    }

    foreach ($refName in $DestinationRefs.Keys) {
        if (-not $SourceRefs.Contains($refName)) {
            $differences.Add("Unexpected destination ref: $refName")
        }
    }

    return $differences.ToArray()
}

function Get-ReconciliationResult {
    param(
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl
    )

    $sourceRefs = Get-ComparableRemoteRefMap $SourceUrl
    $destinationRefs = Get-ComparableRemoteRefMap $DestinationUrl
    $differences = @(Compare-RefMaps $sourceRefs $destinationRefs)
    $missingDestinationRefs = @($differences | Where-Object { $_ -like 'Missing destination ref:*' })
    $unexpectedDestinationRefs = @($differences | Where-Object { $_ -like 'Unexpected destination ref:*' })
    $hashMismatches = @($differences | Where-Object { $_ -like 'Hash mismatch for *' })

    return [ordered]@{
        sourceRefs = $sourceRefs
        destinationRefs = $destinationRefs
        differences = $differences
        exactMatch = $differences.Count -eq 0
        sourceRefCount = $sourceRefs.Count
        destinationRefCount = $destinationRefs.Count
        missingDestinationRefCount = $missingDestinationRefs.Count
        unexpectedDestinationRefCount = $unexpectedDestinationRefs.Count
        hashMismatchCount = $hashMismatches.Count
    }
}

function Assert-RefSnapshotUnchanged {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$Expected,
        [Parameter(Mandatory)][System.Collections.IDictionary]$Actual,
        [Parameter(Mandatory)][string]$Description
    )

    $differences = @(Compare-RefMaps $Expected $Actual)
    if ($differences.Count -gt 0) {
        throw "$Description changed after reconciliation. Run reconciliation again before retrying."
    }
}

function Get-LfsObjectIds {
    param(
        [Parameter(Mandatory)][string]$RepositoryPath,
        [switch]$Bare
    )

    $repositoryArguments = if ($Bare) {
        @("--git-dir=$RepositoryPath")
    }
    else {
        @('-C', $RepositoryPath)
    }
    $lines = @(
        Invoke-NativeCommand git ($repositoryArguments + @('lfs', 'ls-files', '--all', '--long')) -CaptureOutput
    )
    $objectIds = @(
        $lines |
            ForEach-Object {
                if ($_ -match '^([0-9a-fA-F]{64})\s') {
                    $Matches[1].ToLowerInvariant()
                }
                elseif ($_){
                    throw "Unexpected git lfs ls-files output: $_"
                }
            } |
            Sort-Object -Unique
    )

    return $objectIds
}

function Compare-StringSets {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Expected,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Actual,
        [Parameter(Mandatory)][string]$ValueName
    )

    $differences = @(
        Compare-Object -ReferenceObject $Expected -DifferenceObject $Actual |
            ForEach-Object {
                if ($_.SideIndicator -eq '=>') {
                    "Unexpected destination ${ValueName}: $($_.InputObject)"
                }
                else {
                    "Missing destination ${ValueName}: $($_.InputObject)"
                }
            }
    )

    return $differences
}

function Assert-MigrationCanStart {
    param(
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl
    )

    $state = Get-MigrationState
    if ($null -eq $state) {
        return
    }

    Assert-StateIdentity $state $SourceUrl $DestinationUrl
    if (Test-IsPrePushState $state.status) {
        Write-MigrationLog WARN "Retrying after pre-push state '$($state.status)' from run '$($state.runId)'."
        return
    }

    if (Test-IsPostPushState $state.status) {
        throw "A prior migration reached post-push status '$($state.status)' in run '$($state.runId)'. Reconcile source and destination before any further push. State: $script:StatePath"
    }

    throw "Migration state has unsupported status '$($state.status)': $script:StatePath"
}

function Write-MigrationState {
    param(
        [Parameter(Mandatory)][string]$Status,
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl,
        [System.Collections.IDictionary]$Details = @{}
    )

    $previousState = Get-MigrationState
    $history = @()
    $createdAt = (Get-Date).ToString('o')
    if ($null -ne $previousState) {
        Assert-StateIdentity $previousState $SourceUrl $DestinationUrl
        if ($previousState.PSObject.Properties.Name -contains 'history') {
            $history = @($previousState.history)
        }
        if ($previousState.PSObject.Properties.Name -contains 'createdAt') {
            $createdAt = $previousState.createdAt
        }
    }

    $transition = [ordered]@{
        runId = $script:RunId
        status = $Status
        at = (Get-Date).ToString('o')
    }
    if ($Details.Count -gt 0) {
        $transition.details = $Details
    }
    $history += $transition

    Write-JsonFile -Path $script:StatePath -Data ([ordered]@{
        schemaVersion = 2
        migrationIdentity = Get-MigrationIdentity $SourceUrl $DestinationUrl
        runId = $script:RunId
        status = $Status
        source = $SourceUrl
        destination = $DestinationUrl
        owner = $Owner
        usesLfs = [bool]$UsesLfs
        createdAt = $createdAt
        updatedAt = (Get-Date).ToString('o')
        history = $history
    })
}

function Write-RecoveryEvent {
    param(
        [Parameter(Mandatory)][string]$Event,
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl
    )

    $state = Get-MigrationState
    if ($null -eq $state -or -not (Test-IsPostPushState $state.status)) {
        throw 'Recovery events require an existing post-push migration state.'
    }
    Write-MigrationState $state.status $SourceUrl $DestinationUrl @{
        event = $Event
        approver = $Approver
        approvalReference = $ApprovalReference
    }
}

function Invoke-Preflight {
    param(
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl
    )

    Write-MigrationLog INFO 'Starting preflight checks.'
    Assert-CommandAvailable git
    Invoke-NativeCommand git @('--version')

    if ($UsesLfs) {
        Invoke-NativeCommand git @('lfs', 'version')
    }

    Assert-TlsVerification
    Assert-FreeDiskSpace

    $sourceRefs = @(Get-RemoteRefLines $SourceUrl -BranchesAndTagsOnly)
    Write-MigrationLog INFO "Source access confirmed; found $($sourceRefs.Count) branch/tag ref entries."

    $metadata = Get-GitHubRepositoryMetadata $GitHubRepo
    if ($metadata.owner.login -ine $script:GitHubOrganization) {
        throw "Destination repository is not owned by $script:GitHubOrganization."
    }
    if (-not $metadata.private) {
        throw 'Destination GitHub repository must be private.'
    }
    if (-not $metadata.permissions.push) {
        throw 'Authenticated GitHub identity does not have push permission on the destination.'
    }

    $destinationRefs = @(Get-RemoteRefLines $DestinationUrl)
    if ($destinationRefs.Count -ne 0) {
        throw "Destination repository is not empty; found $($destinationRefs.Count) ref entries."
    }

    Write-MigrationLog INFO 'Destination is private, writable, and empty.'
    return $sourceRefs
}

function Invoke-ReconciliationPreflight {
    param(
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl
    )

    Write-MigrationLog INFO 'Starting reconciliation access checks.'
    Assert-CommandAvailable git
    Invoke-NativeCommand git @('--version')
    Assert-TlsVerification

    [void](Get-RemoteRefLines $SourceUrl)
    $metadata = Get-GitHubRepositoryMetadata $GitHubRepo
    if ($metadata.owner.login -ine $script:GitHubOrganization) {
        throw "Destination repository is not owned by $script:GitHubOrganization."
    }
    if (-not $metadata.private) {
        throw 'Destination GitHub repository must be private.'
    }
    if (-not $metadata.permissions.push) {
        throw 'Authenticated GitHub identity does not have push permission on the destination.'
    }
    [void](Get-RemoteRefLines $DestinationUrl)
    Write-MigrationLog INFO 'Reconciliation access checks completed successfully.'
}

function Invoke-Migration {
    param(
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl,
        [Parameter(Mandatory)][System.Collections.IDictionary]$SourceRefs,
        [switch]$ApprovedRetry,
        [switch]$SkipGitPush,
        [System.Collections.IDictionary]$ExpectedSourceRefs,
        [System.Collections.IDictionary]$ExpectedDestinationRefs
    )

    $mirrorPath = Join-Path $script:RunDirectory "$BitbucketRepo.git"
    $verificationPath = Join-Path $script:RunDirectory 'destination-verification'

    Invoke-NativeCommand git @('clone', '--mirror', $SourceUrl, $mirrorPath)
    if ($ApprovedRetry) {
        Write-RecoveryEvent 'RecoveryCloneCompleted' $SourceUrl $DestinationUrl
    }
    else {
        Write-MigrationState 'CloneCompleted' $SourceUrl $DestinationUrl
    }

    $sourceLfsObjectIds = @()
    if ($UsesLfs) {
        Assert-FreeDiskSpace
        Invoke-NativeCommand git @("--git-dir=$mirrorPath", 'lfs', 'fetch', '--all')
        $sourceLfsObjectIds = @(Get-LfsObjectIds $mirrorPath -Bare)
        if ($ApprovedRetry) {
            Write-RecoveryEvent 'RecoveryLfsFetched' $SourceUrl $DestinationUrl
        }
        else {
            Write-MigrationState 'LfsFetched' $SourceUrl $DestinationUrl
        }
        Write-MigrationLog INFO "Fetched $($sourceLfsObjectIds.Count) unique LFS objects from the source."
    }

    if ($ApprovedRetry) {
        $currentSourceRefs = Get-ComparableRemoteRefMap $SourceUrl
        $currentDestinationRefs = Get-ComparableRemoteRefMap $DestinationUrl
        Assert-RefSnapshotUnchanged $ExpectedSourceRefs $currentSourceRefs 'Source refs'
        Assert-RefSnapshotUnchanged $ExpectedDestinationRefs $currentDestinationRefs 'Destination refs'
    }
    else {
        $destinationRefs = @(Get-RemoteRefLines $DestinationUrl)
        if ($destinationRefs.Count -ne 0) {
            throw 'Destination gained refs after preflight. Mirror push has been blocked.'
        }
    }

    if (-not $SkipGitPush) {
        $pushDetails = @{}
        if ($ApprovedRetry) {
            $pushDetails = @{
                approvedRetry = $true
                approver = $Approver
                approvalReference = $ApprovalReference
            }
        }
        Write-MigrationState 'PushStarted' $SourceUrl $DestinationUrl $pushDetails
        Invoke-NativeCommand git @("--git-dir=$mirrorPath", 'push', '--mirror', $DestinationUrl)
        Write-MigrationState 'GitPushCompleted' $SourceUrl $DestinationUrl $pushDetails
    }
    else {
        Write-MigrationLog INFO 'Git refs already match; mirror push is not required.'
    }

    if ($UsesLfs) {
        Invoke-NativeCommand git @("--git-dir=$mirrorPath", 'lfs', 'push', '--all', $DestinationUrl)
        $lfsDetails = @{}
        if ($ApprovedRetry) {
            $lfsDetails = @{
                approvedRetry = $true
                approver = $Approver
                approvalReference = $ApprovalReference
            }
        }
        Write-MigrationState 'LfsPushCompleted' $SourceUrl $DestinationUrl $lfsDetails
    }

    try {
        $destinationRefLines = @(Get-RemoteRefLines $DestinationUrl -BranchesAndTagsOnly)
        $destinationRefMap = ConvertTo-RefMap $destinationRefLines
        $refDifferences = @(Compare-RefMaps $SourceRefs $destinationRefMap)
        if ($refDifferences.Count -gt 0) {
            foreach ($difference in $refDifferences) {
                Write-MigrationLog ERROR $difference
            }
            throw "Source and destination refs differ in $($refDifferences.Count) place(s)."
        }
        Write-MigrationLog INFO "Verified $($SourceRefs.Count) source branch/tag ref entries against the destination."

        $destinationLfsObjectIds = @()
        if ($UsesLfs) {
            $previousSkipSmudge = $env:GIT_LFS_SKIP_SMUDGE
            $env:GIT_LFS_SKIP_SMUDGE = '1'
            try {
                Invoke-NativeCommand git @('clone', '--no-checkout', $DestinationUrl, $verificationPath)
            }
            finally {
                $env:GIT_LFS_SKIP_SMUDGE = $previousSkipSmudge
            }

            Invoke-NativeCommand git @('-C', $verificationPath, 'lfs', 'fetch', '--all')
            Invoke-NativeCommand git @('-C', $verificationPath, 'lfs', 'fsck')
            $destinationLfsObjectIds = @(Get-LfsObjectIds $verificationPath)
            $lfsDifferences = @(Compare-StringSets $sourceLfsObjectIds $destinationLfsObjectIds 'LFS object')
            if ($lfsDifferences.Count -gt 0) {
                foreach ($difference in $lfsDifferences) {
                    Write-MigrationLog ERROR $difference
                }
                throw "Source and destination LFS object inventories differ in $($lfsDifferences.Count) place(s)."
            }
            Write-MigrationLog INFO "Verified $($sourceLfsObjectIds.Count) unique LFS object IDs from the destination."
        }
    }
    catch {
        Write-MigrationState 'VerificationFailed' $SourceUrl $DestinationUrl @{
            error = ConvertTo-SafeText $_.Exception.Message
        }
        throw
    }

    Write-MigrationState 'Verified' $SourceUrl $DestinationUrl
    return [ordered]@{
        sourceRefCount = $SourceRefs.Count
        destinationRefCount = $destinationRefMap.Count
        sourceLfsObjectCount = $sourceLfsObjectIds.Count
        destinationLfsObjectCount = $destinationLfsObjectIds.Count
    }
}

function Invoke-Reconciliation {
    param(
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl
    )

    $state = Get-MigrationState
    if ($null -eq $state) {
        throw "No migration state exists to reconcile: $script:StatePath"
    }
    Assert-StateIdentity $state $SourceUrl $DestinationUrl
    if (-not (Test-IsPostPushState $state.status)) {
        throw "Reconciliation is only required after push begins; current status is '$($state.status)'."
    }

    Invoke-ReconciliationPreflight $SourceUrl $DestinationUrl
    $reconciliation = Get-ReconciliationResult $SourceUrl $DestinationUrl
    foreach ($difference in $reconciliation.differences) {
        Write-MigrationLog WARN $difference
    }
    if ($reconciliation.exactMatch) {
        Write-MigrationLog INFO 'Source and destination refs match exactly.'
    }

    Write-MigrationState $state.status $SourceUrl $DestinationUrl @{
        event = 'Reconciled'
        exactMatch = $reconciliation.exactMatch
        sourceRefCount = $reconciliation.sourceRefCount
        destinationRefCount = $reconciliation.destinationRefCount
        unexpectedDestinationRefCount = $reconciliation.unexpectedDestinationRefCount
    }

    return $reconciliation
}

function Invoke-ApprovedRetry {
    param(
        [Parameter(Mandatory)][string]$SourceUrl,
        [Parameter(Mandatory)][string]$DestinationUrl
    )

    Assert-RecoveryApproval
    $state = Get-MigrationState
    if ($null -eq $state) {
        throw "No migration state exists to retry: $script:StatePath"
    }
    Assert-StateIdentity $state $SourceUrl $DestinationUrl
    if ($state.status -in @('Verified', 'SignedOff')) {
        throw "Migration status '$($state.status)' cannot be retried."
    }
    if (-not (Test-IsPostPushState $state.status)) {
        throw "Use normal migration retry for pre-push status '$($state.status)'."
    }

    Invoke-ReconciliationPreflight $SourceUrl $DestinationUrl
    Assert-FreeDiskSpace
    if ($UsesLfs) {
        Invoke-NativeCommand git @('lfs', 'version')
    }

    $reconciliation = Get-ReconciliationResult $SourceUrl $DestinationUrl
    foreach ($difference in $reconciliation.differences) {
        Write-MigrationLog WARN $difference
    }
    if ($reconciliation.unexpectedDestinationRefCount -gt 0) {
        throw "Retry is blocked because the destination has $($reconciliation.unexpectedDestinationRefCount) ref(s) absent from the source."
    }

    $sourceBranchTagRefs = Get-ComparableRemoteRefMap $SourceUrl -BranchesAndTagsOnly
    $skipGitPush = $reconciliation.exactMatch
    return Invoke-Migration `
        -SourceUrl $SourceUrl `
        -DestinationUrl $DestinationUrl `
        -SourceRefs $sourceBranchTagRefs `
        -ApprovedRetry `
        -SkipGitPush:$skipGitPush `
        -ExpectedSourceRefs $reconciliation.sourceRefs `
        -ExpectedDestinationRefs $reconciliation.destinationRefs
}

function Invoke-Main {
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
    param()

    New-Item -ItemType Directory -Path $script:RunDirectory -Force | Out-Null

    try {
        Assert-SafeRepositoryName $BitbucketWorkspace 'BitbucketWorkspace'
        Assert-SafeRepositoryName $BitbucketRepo 'BitbucketRepo'
        Assert-SafeRepositoryName $GitHubRepo 'GitHubRepo'

        $sourceUrl = "https://bitbucket.org/$BitbucketWorkspace/$BitbucketRepo.git"
        $destinationUrl = "https://github.com/$script:GitHubOrganization/$GitHubRepo.git"

        Write-MigrationLog INFO "Run ID: $script:RunId"
        Write-MigrationLog INFO "Owner: $Owner"
        Write-MigrationLog INFO "Source: $sourceUrl"
        Write-MigrationLog INFO "Destination: $destinationUrl"
        Write-MigrationLog INFO "Uses LFS: $UsesLfs"
        Write-MigrationLog INFO "Operation: $Operation"

        if ($Operation -eq 'Reconcile') {
            $reconciliation = Invoke-Reconciliation $sourceUrl $destinationUrl
            Write-JsonFile -Path $script:ResultPath -Data ([ordered]@{
                runId = $script:RunId
                status = 'Reconciled'
                source = $sourceUrl
                destination = $destinationUrl
                priorMigrationStatus = (Get-MigrationState).status
                exactMatch = $reconciliation.exactMatch
                sourceRefCount = $reconciliation.sourceRefCount
                destinationRefCount = $reconciliation.destinationRefCount
                missingDestinationRefCount = $reconciliation.missingDestinationRefCount
                unexpectedDestinationRefCount = $reconciliation.unexpectedDestinationRefCount
                hashMismatchCount = $reconciliation.hashMismatchCount
                differences = $reconciliation.differences
                completedAt = (Get-Date).ToString('o')
            })
            return
        }

        if ($Operation -eq 'Retry') {
            Assert-RecoveryApproval
            $retryDescription = "retry migration after reconciliation using approval '$ApprovalReference'"
            if (-not $PSCmdlet.ShouldProcess($destinationUrl, $retryDescription)) {
                throw 'Approved retry was not confirmed.'
            }
            $verification = Invoke-ApprovedRetry $sourceUrl $destinationUrl
        }
        elseif ($Operation -eq 'Migrate') {
            Assert-MigrationCanStart $sourceUrl $destinationUrl
            Write-MigrationState 'NotStarted' $sourceUrl $destinationUrl
            $sourceRefLines = Invoke-Preflight $sourceUrl $destinationUrl
            $sourceRefs = ConvertTo-RefMap $sourceRefLines
            Write-MigrationLog INFO 'Preflight completed successfully.'

            $migrationDescription = "mirror $sourceUrl to $destinationUrl"
            if (-not $PSCmdlet.ShouldProcess($destinationUrl, $migrationDescription)) {
                throw 'Migration was not approved.'
            }

            $verification = Invoke-Migration $sourceUrl $destinationUrl $sourceRefs
        }
        else {
            throw "Operation '$Operation' is not implemented."
        }

        Write-MigrationLog INFO 'Migration and verification completed successfully. Owner sign-off remains manual.'

        Write-JsonFile -Path $script:ResultPath -Data ([ordered]@{
            runId = $script:RunId
            status = 'VerifiedPendingOwnerSignOff'
            owner = $Owner
            source = $sourceUrl
            destination = $destinationUrl
            usesLfs = [bool]$UsesLfs
            sourceRefCount = $verification.sourceRefCount
            destinationRefCount = $verification.destinationRefCount
            sourceLfsObjectCount = $verification.sourceLfsObjectCount
            destinationLfsObjectCount = $verification.destinationLfsObjectCount
            operation = $Operation
            approver = if ($Operation -eq 'Retry') { $Approver } else { $null }
            approvalReference = if ($Operation -eq 'Retry') { $ApprovalReference } else { $null }
            completedAt = (Get-Date).ToString('o')
        })
    }
    catch {
        Write-MigrationLog ERROR $_.Exception.Message
        Write-JsonFile -Path $script:ResultPath -Data ([ordered]@{
            runId = $script:RunId
            status = 'Failed'
            owner = $Owner
            error = ConvertTo-SafeText $_.Exception.Message
            failedAt = (Get-Date).ToString('o')
        })
        exit 1
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-Main
}
