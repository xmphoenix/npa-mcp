#requires -RunAsAdministrator
<#
.SYNOPSIS
    Enable Windows OpenSSH Server for the npa-mcp localhost smoke test.

.DESCRIPTION
    - Installs the OpenSSH.Server optional capability when missing.
    - Starts sshd and sets it to Automatic.
    - Adds an inbound firewall rule restricted to 127.0.0.1.
    - Appends a temporary public key to administrators_authorized_keys,
      marked with "npa-mcp-smoke" so it can be removed afterwards.

    Run elevated, for example:
      Start-Process powershell -Verb RunAs -Wait -ArgumentList `
        '-NoProfile','-ExecutionPolicy','Bypass','-File','<this file>','<pubkey>'
#>
param(
    [Parameter(Mandatory = $true)][string]$PublicKeyPath
)

$ErrorActionPreference = "Stop"

Write-Host "== npa-mcp OpenSSH smoke setup =="

$capability = Get-WindowsCapability -Online -Name "OpenSSH.Server*" |
    Select-Object -First 1
if (-not $capability) {
    throw "OpenSSH.Server capability not found on this system."
}
if ($capability.State -ne "Installed") {
    Write-Host "Installing $($capability.Name) (may take a few minutes)..."
    Add-WindowsCapability -Online -Name $capability.Name | Out-Null
}
Write-Host "OpenSSH.Server: installed"

Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd
Write-Host "sshd: started (Automatic)"

$ruleName = "npa-mcp OpenSSH localhost smoke"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule `
    -DisplayName $ruleName `
    -Direction Inbound `
    -Protocol TCP `
    -LocalPort 22 `
    -RemoteAddress 127.0.0.1 `
    -Action Allow `
    -Profile Any | Out-Null
Write-Host "firewall: inbound TCP 22 allowed from 127.0.0.1 only"

if (-not (Test-Path -LiteralPath $PublicKeyPath)) {
    throw "Public key not found: $PublicKeyPath"
}
$publicKey = (Get-Content -LiteralPath $PublicKeyPath -Raw).Trim()
$authFile = Join-Path $env:ProgramData "ssh\administrators_authorized_keys"
$marker = "npa-mcp-smoke"

$existing = @()
if (Test-Path -LiteralPath $authFile) {
    $existing = @(Get-Content -LiteralPath $authFile |
        Where-Object { $_ -and ($_ -notmatch [regex]::Escape($marker)) })
}
$existing += $publicKey
if (-not (Test-Path -LiteralPath (Split-Path $authFile))) {
    New-Item -ItemType Directory -Path (Split-Path $authFile) -Force | Out-Null
}
Set-Content -LiteralPath $authFile -Value $existing -Encoding ascii
icacls $authFile /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
Write-Host "authorized key: appended (marker=$marker)"

Write-Host ""
Get-Service sshd | Select-Object Name, Status, StartType | Format-List
Write-Host "== setup done =="
