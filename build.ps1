<#
    Fabrique le ZIP a distribuer : dist\lwvs-<version>-windows.zip

    Usage :  .\build.ps1
             .\build.ps1 -Clean      (repart de zero)

    Le ZIP contient un dossier lwvs\ avec lwvs.exe et README.txt.
    On distribue un DOSSIER, pas un .exe seul : voir lwvs-gui.spec pour
    pourquoi ce n'est pas du onefile.
#>
[CmdletBinding()]
param([switch]$Clean)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# La version fait foi cote code : elle est affichee dans le titre de la fenetre,
# c'est la seule chose qu'un utilisateur pourra citer en cas de probleme.
$version = (python -c "import lwvs; print(lwvs.__version__)").Trim()
if (-not $version) { throw "version introuvable" }
Write-Host "lwvs $version" -ForegroundColor Cyan

# On ne construit pas un binaire a partir d'un arbre qui ne passe pas ses tests.
# La suite ne fait pas partie du depot publie : absente, on construit quand
# meme, mais en le DISANT -- un build non verifie ne doit pas se faire passer
# pour un build verifie.
if (Test-Path 'tests') {
    Write-Host "`n== tests ==" -ForegroundColor Cyan
    python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "tests en echec : rien n'est empaquete" }
} else {
    Write-Host "`n== tests absents de cet arbre : build NON verifie ==" -ForegroundColor Yellow
}

python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n== installation de PyInstaller ==" -ForegroundColor Cyan
    python -m pip install --quiet "pyinstaller>=6"
    if ($LASTEXITCODE -ne 0) { throw "installation de PyInstaller impossible" }
}

if ($Clean) {
    Write-Host "`n== nettoyage ==" -ForegroundColor Cyan
    foreach ($d in 'build', 'dist') {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d }
    }
}

Write-Host "`n== PyInstaller ==" -ForegroundColor Cyan
python -m PyInstaller --noconfirm lwvs-gui.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller a echoue" }

$app = 'dist\lwvs'
if (-not (Test-Path "$app\lwvs.exe")) { throw "lwvs.exe absent de $app" }
Copy-Item 'packaging\README.txt' $app -Force

# Sanity : le backend C de zstandard est charge par un import dynamique, donc
# invisible a l'analyse statique. S'il manque, le .exe demarre puis echoue au
# premier paquet -- trop tard pour s'en apercevoir.
if (-not (Get-ChildItem $app -Recurse -Filter 'backend_c*.pyd')) {
    throw "zstandard.backend_c absent du build (voir hiddenimports dans le .spec)"
}

$zip = "dist\lwvs-$version-windows.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $app -DestinationPath $zip

$mo = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "`n== pret ==" -ForegroundColor Green
Write-Host "  $zip  ($mo Mo)"
Write-Host "  a tester sur un poste SANS Python et SANS Wireshark :"
Write-Host "  la banniere << Wireshark est requis >> doit s'afficher."
