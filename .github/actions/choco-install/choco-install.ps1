# Installation Chocolatey robuste : 3 tentatives espacées, puis contrôle des commandes attendues.
$ErrorActionPreference = 'Stop'
$packages = $env:CHOCO_PACKAGES -split '\s+' | Where-Object { $_ }
$commands = $env:CHOCO_COMMANDS -split '\s+' | Where-Object { $_ }

function Get-MissingCommands {
    $commands | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) }
}

for ($attempt = 1; $attempt -le 3; $attempt++) {
    choco install @packages --yes --no-progress
    $missing = @(Get-MissingCommands)
    if ($missing.Count -eq 0) {
        foreach ($command in $commands) {
            Write-Host "$command : $((Get-Command $command).Source)"
        }
        exit 0
    }
    Write-Warning "Tentative $attempt : commandes absentes après choco ($($missing -join ', '))."
    if ($attempt -lt 3) { Start-Sleep -Seconds (20 * $attempt) }
}
Write-Error "Chocolatey : $($missing -join ', ') introuvable(s) après 3 tentatives (dépôt communautaire indisponible ?)."
exit 1
