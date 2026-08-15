<#
.SYNOPSIS
    Windows shim for the Makefile. `make` is not available on Windows by default.

.DESCRIPTION
    Mirrors the targets in ./Makefile so the same commands work on the development
    machine and in CI. The Makefile stays authoritative — CI runs on Linux and uses it
    directly. When you add a target there, add it here.

    See ADR-003 in docs/DECISIONS.md.

.EXAMPLE
    .\make.ps1 test
    .\make.ps1 check
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'install', 'up', 'down', 'logs', 'ps', 'test', 'test-unit',
                 'test-integration', 'test-e2e', 'lint', 'fmt', 'typecheck', 'check',
                 'bench', 'cov',
                 'clean', 'nuke')]
    [string]$Target = 'help'
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Invoke-Step {
    param([string[]]$Command)
    Write-Host "-> $($Command -join ' ')" -ForegroundColor DarkCyan
    & $Command[0] @($Command[1..($Command.Length - 1)])
    if ($LASTEXITCODE -ne 0) {
        throw "'$($Command -join ' ')' failed with exit code $LASTEXITCODE"
    }
}

switch ($Target) {
    'help' {
        Write-Host "AgentIAM targets (mirrors ./Makefile):" -ForegroundColor White
        # An ordered hashtable rather than an array of pairs: PowerShell's `@(...)`
        # flattens nested array literals, so `@( @('up','...'), @('down','...') )`
        # collapses into a flat list of strings and `$_[0]` indexes a *character*. That
        # printed one letter per line from T-001 until it was noticed in T-016.
        ([ordered]@{
            'install'          = 'Sync the workspace and install pre-commit hooks'
            'up'               = 'Start infrastructure (Postgres, Redis) and wait for healthy'
            'down'             = 'Stop infrastructure, keeping volumes'
            'logs'             = 'Tail infrastructure logs'
            'ps'               = 'Show infrastructure status'
            'test'             = 'Run the test suite, excluding tests that need infrastructure'
            'test-unit'        = 'Run unit and property tests only'
            'test-integration' = 'Run tests that need Docker (spins its own Postgres)'
            'test-e2e'         = 'Run the end-to-end slice (needs Docker)'
            'lint'             = 'Check formatting and lint rules'
            'fmt'              = 'Apply formatting and autofixable lint rules'
            'typecheck'        = 'Run mypy in strict mode'
            'check'            = 'Everything CI runs'
            'bench'            = 'Run benchmarks (PLAN.md §13.1, NFR-1)'
            'cov'              = 'Run tests with a coverage report'
            'clean'            = 'Remove caches and build artifacts'
            'nuke'             = 'Stop infrastructure and delete its volumes (destroys local data)'
        }).GetEnumerator() | ForEach-Object {
            Write-Host ("  {0,-17} {1}" -f $_.Key, $_.Value) -ForegroundColor Gray
        }
    }
    'install' {
        Invoke-Step @('uv', 'sync')
        Invoke-Step @('uv', 'run', 'pre-commit', 'install')
    }
    'up'        { Invoke-Step @('docker', 'compose', 'up', '-d', '--wait') }
    'down'      { Invoke-Step @('docker', 'compose', 'down') }
    'logs'      { Invoke-Step @('docker', 'compose', 'logs', '-f') }
    'ps'        { Invoke-Step @('docker', 'compose', 'ps') }
    'test'      {
        Invoke-Step @('uv', 'run', 'pytest', '-m',
                      'not integration and not e2e and not chaos and not perf')
    }
    'test-unit' { Invoke-Step @('uv', 'run', 'pytest', 'tests/unit', 'tests/property') }
    'test-integration' {
        # Docker Desktop on Windows does not publish a port for testcontainers' Ryuk
        # reaper, so every container fails to start with a ConnectionError. Disabling
        # Ryuk skips that sidecar; containers are still torn down by the fixtures'
        # context managers. Linux CI does not need this, so it stays out of the Makefile.
        if (-not $env:TESTCONTAINERS_RYUK_DISABLED) { $env:TESTCONTAINERS_RYUK_DISABLED = 'true' }
        Invoke-Step @('uv', 'run', 'pytest', '-m', 'integration')
    }
    'test-e2e' {
        # Same Ryuk workaround as test-integration, and for the same reason.
        if (-not $env:TESTCONTAINERS_RYUK_DISABLED) { $env:TESTCONTAINERS_RYUK_DISABLED = 'true' }
        Invoke-Step @('uv', 'run', 'pytest', '-m', 'e2e')
    }
    'lint' {
        Invoke-Step @('uv', 'run', 'ruff', 'check', '.')
        Invoke-Step @('uv', 'run', 'ruff', 'format', '--check', '.')
    }
    'fmt' {
        Invoke-Step @('uv', 'run', 'ruff', 'format', '.')
        Invoke-Step @('uv', 'run', 'ruff', 'check', '--fix', '.')
    }
    'typecheck' { Invoke-Step @('uv', 'run', 'mypy') }
    'check' {
        & $PSCommandPath 'lint'
        & $PSCommandPath 'typecheck'
        & $PSCommandPath 'test'
    }
    'bench'     { Invoke-Step @('uv', 'run', 'pytest', '-m', 'perf', '--benchmark-only') }
    'cov' {
        Invoke-Step @('uv', 'run', 'pytest', '--cov',
                      '--cov-report=term-missing', '--cov-report=html')
    }
    'clean' {
        @('.pytest_cache', '.mypy_cache', '.ruff_cache', '.hypothesis',
          'htmlcov', '.coverage', 'dist', 'build') | ForEach-Object {
            if (Test-Path $_) { Remove-Item -Recurse -Force $_ }
        }
        Get-ChildItem -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force
        Write-Host 'cleaned' -ForegroundColor DarkGray
    }
    'nuke' {
        Invoke-Step @('docker', 'compose', 'down', '-v')
    }
}
