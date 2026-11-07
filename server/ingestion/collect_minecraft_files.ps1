# Rassemble, sur le PC où le jeu est installé, les fichiers de Minecraft utiles à la phase 3.
# Ne modifie rien dans le dossier du jeu : il copie seulement.
#
# Usage (PowerShell, sur le PC de jeu) :
#   powershell -ExecutionPolicy Bypass -File collect_minecraft_files.ps1 -GameDir "C:\chemin\vers\.minecraft"
# Sans -GameDir, le dossier par défaut %APPDATA%\.minecraft est utilisé.
#
# Résultat : un dossier minecraft-26.2\ à côté du script, contenant :
#   26.2.jar      le jeu (recettes, en_us.json, version.json)
#   26.2.json     la description de la version (indique l'index des ressources)
#   fr_fr.json    les noms français, extraits des ressources (assets)
# Copie ce dossier sur le PC de l'API, dans server\data\minecraft\26.2\
param(
    [string]$GameDir = "$env:APPDATA\.minecraft",
    [string]$Version = "26.2"
)
$ErrorActionPreference = "Stop"
$out = Join-Path $PSScriptRoot "minecraft-$Version"
New-Item -ItemType Directory -Force $out | Out-Null

$versionDir = Join-Path $GameDir "versions\$Version"
foreach ($f in "$Version.jar", "$Version.json") {
    $src = Join-Path $versionDir $f
    if (-not (Test-Path $src)) { throw "Introuvable : $src (vérifie -GameDir ; le launcher doit avoir lancé le jeu en $Version au moins une fois)" }
    Copy-Item $src $out
}

# fr_fr.json n'est pas dans le jar : il est rangé dans assets\objects sous son empreinte.
$manifest = Get-Content (Join-Path $versionDir "$Version.json") -Raw | ConvertFrom-Json
$indexId = $manifest.assetIndex.id
$index = Get-Content (Join-Path $GameDir "assets\indexes\$indexId.json") -Raw | ConvertFrom-Json
$hash = $index.objects.'minecraft/lang/fr_fr.json'.hash
if (-not $hash) { throw "fr_fr.json absent de l'index des ressources $indexId" }
Copy-Item (Join-Path $GameDir "assets\objects\$($hash.Substring(0, 2))\$hash") (Join-Path $out "fr_fr.json")

Get-ChildItem $out | Select-Object Name, @{n = "Taille (Ko)"; e = { [int]($_.Length / 1KB) } } | Format-Table
Write-Host "Copie le dossier $out sur le PC de l'API, dans server\data\minecraft\$Version\"
