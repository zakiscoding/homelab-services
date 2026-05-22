# Generate homelab-services/.env from .env.example (never committed).
param(
    [string]$Domain = "",
    [string]$SiteLabel = "ml-zakis-console"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

if (Test-Path ".env") {
    Write-Host ".env already exists - delete it first to regenerate, or edit in place."
    exit 0
}

function New-Hex([int]$bytes = 24) {
    $b = New-Object byte[] $bytes
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    -join ($b | ForEach-Object { $_.ToString("x2") })
}

$apiKey = New-Hex 24
$workerKey = New-Hex 24
$pgPass = New-Hex 16
$gfPass = New-Hex 12
$webUser = "homelab"
$webPass = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 16 | ForEach-Object { [char]$_ })

Copy-Item ".env.example" ".env"
$content = Get-Content ".env" -Raw
$content = $content.Replace('change-me-long-random-string', $apiKey)
$content = $content.Replace('change-me-worker-key-different-from-api-key', $workerKey)
$content = $content.Replace('change-me-strong', $pgPass)
$content = $content.Replace('GRAFANA_PASSWORD=change-me', "GRAFANA_PASSWORD=$gfPass")

$lines = @()
if ($Domain) {
    $lines += ""
    $lines += "# --- Public HTTPS ($SiteLabel) ---"
    $lines += "WEB_AUTH_USER=$webUser"
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $hash = (docker run --rm caddy:2-alpine caddy hash-password --plaintext $webPass 2>$null).Trim()
        if ($hash) { $lines += "WEB_AUTH_HASH='$hash'" }
    }
    $lines += "DOMAIN=$Domain"
    $lines += "ACME_EMAIL=admin@$Domain"
    $lines += "# Browser password: $webPass"
}

if ($lines.Count -gt 0) {
    $content = $content.TrimEnd() + [Environment]::NewLine + ($lines -join [Environment]::NewLine) + [Environment]::NewLine
} else {
    $content = $content.TrimEnd() + [Environment]::NewLine
}
Set-Content -Path ".env" -Value $content -Encoding utf8

Write-Host "Created $Root\.env"
if ($Domain) {
    Write-Host "  Web login: $webUser / $webPass"
} else {
    Write-Host "  UI: http://100.91.113.98:3080 (Tailscale, no domain)"
}

$WebRoot = Join-Path (Split-Path $Root -Parent) "homelab-web"
if (Test-Path $WebRoot) {
    @(
        "HOMELAB_API_URL=http://100.91.113.98",
        "HOMELAB_API_KEY=$apiKey"
    ) | Set-Content -Path (Join-Path $WebRoot ".env.local") -Encoding utf8
    Write-Host "  Wrote homelab-web\.env.local"
}
Write-Host "Copy .env to iMac: ~/1tb-drive/homelab-services/.env"
