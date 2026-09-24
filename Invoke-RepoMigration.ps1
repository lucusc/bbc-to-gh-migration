[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$BitbucketWorkspace,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$BitbucketRepo,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$GitHubRepo,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$Owner,

    [switch]$UsesLfs,

    [ValidateNotNullOrEmpty()]
    [string]$WorkingDirectory = (Join-Path $PSScriptRoot 'migration-work'),

    [ValidateRange(1, 10240)]
    [int]$MinimumFreeSpaceGB = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:GitHubOrganization = 'bcgov-c'
$script:RunId = Get-Date -Format 'yyyyMMdd-HHmmss'
$script:RunDirectory = Join-Path $WorkingDirectory "$BitbucketWorkspace--$BitbucketRepo\$script:RunId"
$script:LogPath = Join-Path $script:RunDirectory 'migration.log'
$script:ResultPath = Join-Path $script:RunDirectory 'result.json'

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
    Write-Host $entry
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
    $scopeArguments = @(
        @('config', '--system', '--get', 'http.sslVerify'),
        @('config', '--global', '--get', 'http.sslVerify')
    )

    foreach ($arguments in $scopeArguments) {
        $value = & git @arguments 2>$null
        if ($LASTEXITCODE -eq 0 -and "$value".Trim() -ieq 'false') {
            throw "Git TLS certificate verification is disabled in $($arguments[1].TrimStart('-')) configuration."
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

function Test-RemoteRefs {
    param([Parameter(Mandatory)][string]$RemoteUrl)

    return @(Invoke-NativeCommand git @('ls-remote', '--heads', '--tags', $RemoteUrl) -CaptureOutput)
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

    $sourceRefs = Test-RemoteRefs $SourceUrl
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

    $destinationRefs = Test-RemoteRefs $DestinationUrl
    if ($destinationRefs.Count -ne 0) {
        throw "Destination repository is not empty; found $($destinationRefs.Count) branch/tag ref entries."
    }

    Write-MigrationLog INFO 'Destination is private, writable, and empty.'
    return $sourceRefs
}

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

    $sourceRefs = Invoke-Preflight $sourceUrl $destinationUrl
    Write-MigrationLog INFO 'Preflight completed successfully.'

    [ordered]@{
        runId = $script:RunId
        status = 'PreflightCompleted'
        owner = $Owner
        source = $sourceUrl
        destination = $destinationUrl
        usesLfs = [bool]$UsesLfs
        sourceRefCount = $sourceRefs.Count
        completedAt = (Get-Date).ToString('o')
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $script:ResultPath -Encoding utf8
}
catch {
    Write-MigrationLog ERROR $_.Exception.Message
    [ordered]@{
        runId = $script:RunId
        status = 'Failed'
        owner = $Owner
        error = ConvertTo-SafeText $_.Exception.Message
        failedAt = (Get-Date).ToString('o')
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $script:ResultPath -Encoding utf8
    exit 1
}

