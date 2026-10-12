# Downloads a portable Python (python-build-standalone, includes tkinter) into <Studio>\studio-data\python.
# Called by Start-Studio.bat only when no suitable Python is installed. Writes nothing outside the Studio folder.
param([Parameter(Mandatory = $true)][string]$Dest)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$downloads = Join-Path $Dest 'downloads'
New-Item -ItemType Directory -Force -Path $downloads | Out-Null
$api = 'https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest'
$release = Invoke-RestMethod -UseBasicParsing -Uri $api -Headers @{ 'User-Agent' = 'AniimoPalworldStudio' }
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
$pattern = "^cpython-3\.12\.\d+\+\d+-$arch-pc-windows-msvc-install_only\.tar\.gz$"
$asset = $release.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
if (-not $asset) { throw "No portable Python 3.12 build found for $arch in release $($release.tag_name)" }
$archive = Join-Path $downloads $asset.name
Write-Host "Downloading $($asset.name) ..."
Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $archive
Write-Host "Extracting ..."
tar -xzf $archive -C $Dest
Remove-Item $archive
if (-not (Test-Path (Join-Path $Dest 'python\python.exe'))) { throw 'Extraction did not produce python\python.exe' }
Write-Host 'Portable Python ready.'
