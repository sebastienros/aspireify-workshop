[CmdletBinding()]
param(
    [ValidateSet('auto', 'podman', 'docker')]
    [string] $Runtime = 'auto'
)
. "$PSScriptRoot/common.ps1"
Initialize-Runtime
Test-Application
