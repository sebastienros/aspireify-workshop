[CmdletBinding()]
param(
    [ValidateSet('auto', 'podman', 'docker')]
    [string] $Runtime = 'auto'
)
. "$PSScriptRoot/common.ps1"

$script:AdminProcess = $null
$script:FrontendProcess = $null
$containersStarted = $false
$originalDirectory = Get-Location
$environmentNames = @(
    'ConnectionStrings__db', 'ConnectionStrings__cache', 'Authentication__AdminPassword',
    'Aspire__UseServiceDefaults', 'ASPNETCORE_ENVIRONMENT', 'DOTNET_ENVIRONMENT',
    'ASPNETCORE_URLS', 'BINGO_ADMIN_URL'
)
$originalEnvironment = @{}
foreach ($name in $environmentNames) {
    $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

try {
    foreach ($tool in @('dotnet', 'node', 'npm')) { Assert-Command $tool }
    Initialize-Runtime
    foreach ($port in @(5432, 6379, 5039, 5173)) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            try { $client.Connect('localhost', $port) }
            catch [System.Net.Sockets.SocketException] {
                if ($_.Exception.SocketErrorCode -ne [System.Net.Sockets.SocketError]::ConnectionRefused) { throw }
            }
            if ($client.Connected) { throw "Port $port is already in use. Stop the conflicting service before starting." }
        }
        finally { $client.Dispose() }
    }

    Set-Location $StartDirectory
    Invoke-Native dotnet @('build', 'AspireifyBingo.slnx', '--nologo')
    Set-Location "$StartDirectory/src/bingo-board"
    Invoke-Native npm @('ci')
    Set-Location $StartDirectory

    $env:ConnectionStrings__db = 'Host=localhost;Port=5432;Database=bingo;Username=postgres;Password=postgres'
    $env:ConnectionStrings__cache = 'localhost:6379'
    if (-not $env:Authentication__AdminPassword) { $env:Authentication__AdminPassword = 'admin' }
    $env:Aspire__UseServiceDefaults = 'false'
    $env:ASPNETCORE_ENVIRONMENT = 'Development'
    $env:DOTNET_ENVIRONMENT = 'Development'
    $env:ASPNETCORE_URLS = 'http://localhost:5039'
    $env:BINGO_ADMIN_URL = 'http://localhost:5039'
    $logDirectory = "$StartDirectory/.script-state"
    New-Item $logDirectory -ItemType Directory -Force | Out-Null

    $containersStarted = $true
    Invoke-Compose @('up', '-d')
    Wait-Service 'PostgreSQL' { Test-Postgres }
    Wait-Service 'Redis' { Test-Redis }
    Set-Location "$StartDirectory/src/BingoBoard.MigrationService"
    Invoke-Native dotnet @('bin/Debug/net10.0/BingoBoard.MigrationService.dll')
    Set-Location $StartDirectory

    $script:AdminProcess = Start-Process -FilePath (Get-Command dotnet).Source `
        -ArgumentList 'bin/Debug/net10.0/BingoBoard.Admin.dll' `
        -WorkingDirectory "$StartDirectory/src/BingoBoard.Admin" `
        -RedirectStandardOutput "$logDirectory/admin.log" -RedirectStandardError "$logDirectory/admin.error.log" `
        -NoNewWindow -PassThru
    Wait-Service 'admin backend' { Test-Version 'http://localhost:5039' }
    $script:FrontendProcess = Start-Process -FilePath (Get-Command node).Source `
        -ArgumentList 'node_modules/vite/bin/vite.js', '--host', 'localhost', '--port', '5173', '--strictPort' `
        -WorkingDirectory "$StartDirectory/src/bingo-board" `
        -RedirectStandardOutput "$logDirectory/frontend.log" -RedirectStandardError "$logDirectory/frontend.error.log" `
        -NoNewWindow -PassThru
    Wait-Service 'player frontend' { Test-Http 'http://localhost:5173/' }
    Test-Application

    Write-Host 'Player: http://localhost:5173 | Admin: http://localhost:5039 (user: admin)'
    Write-Host "Logs: $logDirectory | Press Ctrl+C to stop; database data is preserved."
    while (-not $script:AdminProcess.HasExited -and -not $script:FrontendProcess.HasExited) {
        Start-Sleep -Seconds 1
    }
    throw 'An application process exited unexpectedly. Inspect .script-state/*.log.'
}
finally {
    try {
        foreach ($process in @($script:FrontendProcess, $script:AdminProcess)) {
            if ($null -ne $process -and -not $process.HasExited) {
                Stop-Process -Id $process.Id
                $process.WaitForExit()
            }
        }
    }
    finally {
        try {
            if ($containersStarted) { Invoke-Compose @('stop') }
        }
        finally {
            Set-Location $originalDirectory
            foreach ($name in $environmentNames) {
                [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process')
            }
        }
    }
}
