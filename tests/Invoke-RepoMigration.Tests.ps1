BeforeAll {
    $scriptPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'Invoke-RepoMigration.ps1'
    . $scriptPath `
        -BitbucketWorkspace 'test-workspace' `
        -BitbucketRepo 'test-source' `
        -GitHubRepo 'test-destination' `
        -Owner 'test-owner' `
        -WorkingDirectory $TestDrive

    function New-LocalRepositoryPair {
        param(
            [Parameter(Mandatory)][string]$Root,
            [switch]$PopulateDestination
        )

        $sourceWork = Join-Path $Root 'source-work'
        $sourceBare = Join-Path $Root 'source.git'
        $destinationBare = Join-Path $Root 'destination.git'
        New-Item -ItemType Directory -Path $Root -Force | Out-Null

        & git init --quiet $sourceWork
        & git -C $sourceWork config user.name 'Migration Test'
        & git -C $sourceWork config user.email 'migration-test@example.invalid'
        Set-Content -LiteralPath (Join-Path $sourceWork 'README.md') -Value '# Test repository'
        & git -C $sourceWork add README.md
        & git -C $sourceWork commit --quiet -m 'Initial commit'
        & git -C $sourceWork branch feature
        & git -C $sourceWork tag v1
        & git clone --quiet --bare $sourceWork $sourceBare
        & git init --quiet --bare $destinationBare

        if ($PopulateDestination) {
            & git "--git-dir=$sourceBare" push --quiet --mirror $destinationBare
        }

        return @{
            sourceWork = $sourceWork
            sourceBare = $sourceBare
            destinationBare = $destinationBare
        }
    }
}

Describe 'ConvertTo-SafeText' {
    It 'removes URL credentials and authorization values' {
        $text = 'https://user:secret@example.test/repo Authorization: Bearer top-secret'
        $safeText = ConvertTo-SafeText $text

        $safeText | Should -Be 'https://example.test/repo Authorization: [REDACTED]'
    }

    It 'redacts recognizable GitHub tokens' {
        ConvertTo-SafeText 'token=ghp_abcdefghijklmnopqrstuvwxyz0123456789' |
            Should -Be 'token=[REDACTED]'
    }
}

Describe 'Git ref comparison' {
    It 'parses branch and annotated tag refs' {
        $refs = ConvertTo-RefMap @(
            "1111111111111111111111111111111111111111`trefs/heads/main"
            "2222222222222222222222222222222222222222`trefs/tags/v1"
            "3333333333333333333333333333333333333333`trefs/tags/v1^{}"
        )

        $refs.Count | Should -Be 3
        $refs['refs/heads/main'] | Should -Be '1111111111111111111111111111111111111111'
        $refs['refs/tags/v1^{}'] | Should -Be '3333333333333333333333333333333333333333'
    }

    It 'reports missing, unexpected, and mismatched refs' {
        $source = [ordered]@{
            'refs/heads/main' = '1111111111111111111111111111111111111111'
            'refs/tags/v1' = '2222222222222222222222222222222222222222'
        }
        $destination = [ordered]@{
            'refs/heads/main' = '9999999999999999999999999999999999999999'
            'refs/heads/extra' = '3333333333333333333333333333333333333333'
        }

        $differences = @(Compare-RefMaps $source $destination)

        $differences | Should -HaveCount 3
        $differences | Should -Contain 'Missing destination ref: refs/tags/v1'
        $differences | Should -Contain 'Unexpected destination ref: refs/heads/extra'
        $differences | Should -Contain 'Hash mismatch for refs/heads/main: source=1111111111111111111111111111111111111111 destination=9999999999999999999999999999999999999999'
    }

    It 'accepts identical ref maps' {
        $source = [ordered]@{
            'refs/heads/main' = '1111111111111111111111111111111111111111'
        }
        $destination = [ordered]@{
            'refs/heads/main' = '1111111111111111111111111111111111111111'
        }

        @(Compare-RefMaps $source $destination) | Should -HaveCount 0
    }
}

Describe 'LFS object comparison' {
    It 'reports missing and unexpected object IDs' {
        $differences = @(Compare-StringSets `
            -Expected @('aaa', 'bbb') `
            -Actual @('bbb', 'ccc') `
            -ValueName 'LFS object')

        $differences | Should -HaveCount 2
        $differences | Should -Contain 'Missing destination LFS object: aaa'
        $differences | Should -Contain 'Unexpected destination LFS object: ccc'
    }

    It 'accepts empty inventories' {
        @(Compare-StringSets -Expected @() -Actual @() -ValueName 'LFS object') |
            Should -HaveCount 0
    }
}

Describe 'Persistent migration state' {
    BeforeAll {
        $script:RunDirectory = Join-Path $TestDrive 'state-run'
        $script:LogPath = Join-Path $script:RunDirectory 'migration.log'
        New-Item -ItemType Directory -Path $script:RunDirectory -Force | Out-Null
    }

    It 'allows a repository with no migration state' {
        $script:StatePath = Join-Path $TestDrive 'missing-state.json'
        {
            Assert-MigrationCanStart `
                'https://bitbucket.org/test-workspace/test-source.git' `
                'https://github.com/bcgov-c/test-destination.git'
        } | Should -Not -Throw
    }

    It 'allows retry after a pre-push state' {
        $script:StatePath = Join-Path $TestDrive 'migration-state.json'
        $sourceUrl = 'https://bitbucket.org/test-workspace/test-source.git'
        $destinationUrl = 'https://github.com/bcgov-c/test-destination.git'
        Write-MigrationState 'CloneCompleted' $sourceUrl $destinationUrl

        { Assert-MigrationCanStart $sourceUrl $destinationUrl } | Should -Not -Throw
    }

    It 'blocks a repository with prior push state' {
        $script:StatePath = Join-Path $TestDrive 'post-push-state.json'
        $sourceUrl = 'https://bitbucket.org/test-workspace/test-source.git'
        $destinationUrl = 'https://github.com/bcgov-c/test-destination.git'
        Write-MigrationState 'PushStarted' $sourceUrl $destinationUrl

        { Assert-MigrationCanStart $sourceUrl $destinationUrl } |
            Should -Throw "*status 'PushStarted'*reconcile*"
    }

    It 'preserves transition history' {
        $script:StatePath = Join-Path $TestDrive 'history-state.json'
        $sourceUrl = 'https://bitbucket.org/test-workspace/test-source.git'
        $destinationUrl = 'https://github.com/bcgov-c/test-destination.git'
        Write-MigrationState 'NotStarted' $sourceUrl $destinationUrl
        Write-MigrationState 'CloneCompleted' $sourceUrl $destinationUrl

        $state = Get-MigrationState
        $state.schemaVersion | Should -Be 2
        $state.status | Should -Be 'CloneCompleted'
        $state.history | Should -HaveCount 2
        $state.history[0].status | Should -Be 'NotStarted'
        $state.history[1].status | Should -Be 'CloneCompleted'
    }

    It 'rejects state for a different migration identity' {
        $script:StatePath = Join-Path $TestDrive 'identity-state.json'
        $sourceUrl = 'https://bitbucket.org/test-workspace/test-source.git'
        $destinationUrl = 'https://github.com/bcgov-c/test-destination.git'
        Write-MigrationState 'CloneCompleted' $sourceUrl $destinationUrl

        {
            Assert-MigrationCanStart `
                'https://bitbucket.org/test-workspace/other-source.git' `
                $destinationUrl
        } | Should -Throw '*different source, destination, owner, or LFS setting*'
    }
}

Describe 'Repository name validation' {
    It 'rejects traversal and credential URL characters' {
        { Assert-SafeRepositoryName '../escape' 'Repository' } | Should -Throw
        { Assert-SafeRepositoryName 'repo@host' 'Repository' } | Should -Throw
    }

    It 'accepts standard repository names' {
        { Assert-SafeRepositoryName 'repo-name_1.2' 'Repository' } | Should -Not -Throw
    }
}

Describe 'Reconciliation' {
    It 'detects destination-only refs that make a mirror retry unsafe' {
        $repositories = New-LocalRepositoryPair `
            -Root (Join-Path $TestDrive 'reconciliation') `
            -PopulateDestination
        $mainHash = & git "--git-dir=$($repositories.sourceBare)" rev-parse refs/heads/main
        & git "--git-dir=$($repositories.destinationBare)" update-ref refs/heads/destination-only $mainHash

        $result = Get-ReconciliationResult `
            $repositories.sourceBare `
            $repositories.destinationBare

        $result.exactMatch | Should -BeFalse
        $result.unexpectedDestinationRefCount | Should -Be 1
        $result.differences | Should -Contain 'Unexpected destination ref: refs/heads/destination-only'
    }

    It 'detects refs missing from the destination' {
        $repositories = New-LocalRepositoryPair `
            -Root (Join-Path $TestDrive 'missing-destination-ref') `
            -PopulateDestination
        & git "--git-dir=$($repositories.destinationBare)" update-ref -d refs/heads/feature

        $result = Get-ReconciliationResult `
            $repositories.sourceBare `
            $repositories.destinationBare

        $result.missingDestinationRefCount | Should -Be 1
        $result.unexpectedDestinationRefCount | Should -Be 0
        $result.differences | Should -Contain 'Missing destination ref: refs/heads/feature'
    }
}

Describe 'Approved retry' {
    It 'restores missing refs and preserves post-push state during recovery' {
        $repositories = New-LocalRepositoryPair `
            -Root (Join-Path $TestDrive 'approved-retry') `
            -PopulateDestination
        & git "--git-dir=$($repositories.destinationBare)" update-ref -d refs/heads/feature

        $script:RunDirectory = Join-Path $TestDrive 'approved-retry-run'
        $script:LogPath = Join-Path $script:RunDirectory 'migration.log'
        $script:StatePath = Join-Path $TestDrive 'approved-retry-state.json'
        New-Item -ItemType Directory -Path $script:RunDirectory -Force | Out-Null
        Write-MigrationState `
            'PushStarted' `
            $repositories.sourceBare `
            $repositories.destinationBare

        $reconciliation = Get-ReconciliationResult `
            $repositories.sourceBare `
            $repositories.destinationBare
        $sourceBranchTagRefs = Get-ComparableRemoteRefMap `
            $repositories.sourceBare `
            -BranchesAndTagsOnly

        $result = Invoke-Migration `
            -SourceUrl $repositories.sourceBare `
            -DestinationUrl $repositories.destinationBare `
            -SourceRefs $sourceBranchTagRefs `
            -ApprovedRetry `
            -ExpectedSourceRefs $reconciliation.sourceRefs `
            -ExpectedDestinationRefs $reconciliation.destinationRefs `
            -RecoveryApprover 'test-approver' `
            -RecoveryApprovalReference 'CHANGE-123'

        $result.sourceRefCount | Should -Be 3
        $state = Get-MigrationState
        $state.status | Should -Be 'Verified'
        $events = @(
            $state.history |
                Where-Object {
                    $_.PSObject.Properties.Name -contains 'details' -and
                    $_.details.PSObject.Properties.Name -contains 'event'
                } |
                ForEach-Object { $_.details.event }
        )
        $events | Should -Contain 'RecoveryCloneCompleted'
        $state.history.status | Should -Not -Contain 'CloneCompleted'
        (Get-ReconciliationResult `
            $repositories.sourceBare `
            $repositories.destinationBare).exactMatch | Should -BeTrue
    }
}

Describe 'Owner sign-off' {
    BeforeEach {
        $script:RunDirectory = Join-Path $TestDrive 'sign-off-run'
        $script:LogPath = Join-Path $script:RunDirectory 'migration.log'
        $script:StatePath = Join-Path $TestDrive 'sign-off-state.json'
        $script:SignOffPath = Join-Path $TestDrive 'owner-sign-off.json'
        New-Item -ItemType Directory -Path $script:RunDirectory -Force | Out-Null
    }

    It 'records sign-off only after technical verification' {
        $sourceUrl = 'https://bitbucket.org/test-workspace/test-source.git'
        $destinationUrl = 'https://github.com/bcgov-c/test-destination.git'
        Write-MigrationState 'Verified' $sourceUrl $destinationUrl

        $signOff = Invoke-OwnerSignOff `
            $sourceUrl `
            $destinationUrl `
            'repository-owner' `
            'CHANGE-456' `
            'Migration accepted.'

        $signOff.status | Should -Be 'Approved'
        $signOff.approver | Should -Be 'repository-owner'
        $signOff.approvalReference | Should -Be 'CHANGE-456'
        (Get-MigrationState).status | Should -Be 'SignedOff'
        Test-Path -LiteralPath $script:SignOffPath | Should -BeTrue
    }

    It 'rejects sign-off when verification has failed' {
        $sourceUrl = 'https://bitbucket.org/test-workspace/test-source.git'
        $destinationUrl = 'https://github.com/bcgov-c/test-destination.git'
        Write-MigrationState 'VerificationFailed' $sourceUrl $destinationUrl

        {
            Invoke-OwnerSignOff `
                $sourceUrl `
                $destinationUrl `
                'repository-owner' `
                'CHANGE-456' `
                $null
        } |
            Should -Throw "*requires migration status 'Verified'*"
    }
}

Describe 'Non-LFS mirror migration' {
    It 'mirrors and verifies branches and tags between local bare repositories' {
        $repositories = New-LocalRepositoryPair -Root (Join-Path $TestDrive 'initial-migration')
        $sourceBare = $repositories.sourceBare
        $destinationBare = $repositories.destinationBare
        $script:RunDirectory = Join-Path $TestDrive 'integration-run'
        $script:LogPath = Join-Path $script:RunDirectory 'migration.log'
        $script:StatePath = Join-Path $TestDrive 'integration-state.json'
        New-Item -ItemType Directory -Path $script:RunDirectory -Force | Out-Null

        $sourceRefs = ConvertTo-RefMap @(& git ls-remote --heads --tags $sourceBare)
        $result = Invoke-Migration $sourceBare $destinationBare $sourceRefs

        $result.sourceRefCount | Should -Be 3
        $result.destinationRefCount | Should -Be 3
        $state = Get-Content -LiteralPath $script:StatePath -Raw | ConvertFrom-Json
        $state.status | Should -Be 'Verified'

        $destinationRefs = ConvertTo-RefMap @(& git ls-remote --heads --tags $destinationBare)
        @(Compare-RefMaps $sourceRefs $destinationRefs) | Should -HaveCount 0
    }
}
