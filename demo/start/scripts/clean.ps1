[CmdletBinding()]
param(
    [ValidateSet('auto', 'podman', 'docker')]
    [string] $Runtime = 'auto',
    [switch] $Reset
)
. "$PSScriptRoot/common.ps1"
Initialize-Runtime
if ($Reset) {
    Invoke-Compose @('down', '--volumes')
}
else {
    Invoke-Compose @('stop')
}
