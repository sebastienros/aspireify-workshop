Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$StartDirectory = Split-Path $PSScriptRoot -Parent

function Assert-Command([string] $Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Invoke-Native([string] $Command, [string[]] $Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE."
    }
}

function Initialize-Runtime {
    if ($Runtime -eq 'auto') {
        if (Get-Command podman -ErrorAction SilentlyContinue) {
            $script:Runtime = 'podman'
        }
        elseif (Get-Command docker -ErrorAction SilentlyContinue) {
            $script:Runtime = 'docker'
        }
        else {
            throw 'Neither Podman nor Docker is installed. Install one with Compose support.'
        }
    }
    Assert-Command $Runtime
    Write-Host "Using container runtime: $Runtime"
    Invoke-Native $Runtime @('info') | Out-Null
    Invoke-Native $Runtime @('compose', 'version') | Out-Null
}

function Invoke-Compose([string[]] $Arguments) {
    Invoke-Native $Runtime (@('compose', '--project-directory', $StartDirectory, '-f', "$StartDirectory/compose.yaml") + $Arguments)
}

function Test-Postgres {
    Invoke-Compose @('exec', '-T', 'postgres', 'pg_isready', '-U', 'postgres', '-d', 'bingo') | Out-Null
}

function Test-Redis {
    $reply = Invoke-Compose @('exec', '-T', 'redis', 'redis-cli', 'ping')
    if (($reply -join "`n").Trim() -ne 'PONG') { throw 'Redis did not return PONG.' }
}

function Test-SeedData {
    $query = 'SELECT EXISTS (SELECT 1 FROM "AspNetUsers" WHERE "UserName" = ''admin'') AND EXISTS (SELECT 1 FROM "BingoSquares");'
    $result = Invoke-Compose @('exec', '-T', 'postgres', 'psql', '-U', 'postgres', '-d', 'bingo', '-At', '-v', 'ON_ERROR_STOP=1', '-c', $query)
    if (($result -join "`n").Trim() -ne 't') { throw 'Admin user or bingo squares are missing.' }
}

function Test-Http([string] $Url) {
    $response = Invoke-WebRequest $Url -TimeoutSec 5
    if ($response.StatusCode -ne 200) { throw "Unexpected HTTP status from $Url." }
}

function Test-Version([string] $Url) {
    $data = Invoke-RestMethod "$Url/api/version-info" -TimeoutSec 5
    if (-not $data.dotNetVersion -or $data.aspireVersion -ne 'not configured') {
        throw "Invalid non-Aspire version response from $Url."
    }
}

function Test-SignalR([string] $Url) {
    $data = Invoke-RestMethod "$Url/bingohub/negotiate?negotiateVersion=1" -Method Post -TimeoutSec 5
    if (-not $data.connectionToken -or -not $data.availableTransports.Count) {
        throw "Invalid SignalR negotiation response from $Url."
    }
}

function Wait-Service([string] $Label, [scriptblock] $Test) {
    $lastFailure = ''
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            & $Test
            return
        }
        catch {
            $lastFailure = $_.Exception.Message
        }
        foreach ($process in @($script:AdminProcess, $script:FrontendProcess)) {
            if ($null -ne $process -and $process.HasExited) {
                throw "An application process exited. Inspect $StartDirectory/.script-state/*.log."
            }
        }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for ${Label}: $lastFailure"
}

function Test-Application {
    $checks = [ordered]@{
        'PostgreSQL is accepting connections' = { Test-Postgres }
        'Redis responds to PING' = { Test-Redis }
        'Migrations and seed data are present' = { Test-SeedData }
        'Admin portal' = { Test-Http 'http://localhost:5039/login' }
        'Admin API (without Aspire)' = { Test-Version 'http://localhost:5039' }
        'Player frontend' = { Test-Http 'http://localhost:5173/' }
        'Frontend API proxy' = { Test-Version 'http://localhost:5173' }
        'Frontend SignalR proxy' = { Test-SignalR 'http://localhost:5173' }
    }
    $failed = $false
    foreach ($entry in $checks.GetEnumerator()) {
        try {
            & $entry.Value
            Write-Host "OK: $($entry.Key)"
        }
        catch {
            Write-Host "FAIL: $($entry.Key): $($_.Exception.Message)" -ForegroundColor Red
            $failed = $true
        }
    }
    if ($failed) { throw 'One or more application checks failed.' }
}
