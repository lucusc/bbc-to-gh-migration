BeforeAll {
    $scriptPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'Invoke-RepoMigration.ps1'
    . $scriptPath `
        -BitbucketWorkspace 'test-workspace' `
        -BitbucketRepo 'test-source' `
        -GitHubRepo 'test-destination' `
        -Owner 'test-owner' `
        -WorkingDirectory $TestDrive
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

Describe 'Persistent rerun guard' {
    It 'allows a repository with no migration state' {
        $script:StatePath = Join-Path $TestDrive 'missing-state.json'
        { Assert-NoPriorPush } | Should -Not -Throw
    }

    It 'blocks any repository with prior push state' {
        $script:StatePath = Join-Path $TestDrive 'migration-state.json'
        @{
            runId = 'prior-run'
            status = 'PushStarted'
        } | ConvertTo-Json | Set-Content -LiteralPath $script:StatePath

        { Assert-NoPriorPush } |
            Should -Throw "*status 'PushStarted'*reconcile*"
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

Describe 'Non-LFS mirror migration' {
    It 'mirrors and verifies branches and tags between local bare repositories' {
        $sourceWork = Join-Path $TestDrive 'source-work'
        $sourceBare = Join-Path $TestDrive 'source.git'
        $destinationBare = Join-Path $TestDrive 'destination.git'
        $script:RunDirectory = Join-Path $TestDrive 'integration-run'
        $script:LogPath = Join-Path $script:RunDirectory 'migration.log'
        $script:StatePath = Join-Path $TestDrive 'integration-state.json'
        New-Item -ItemType Directory -Path $script:RunDirectory -Force | Out-Null

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
