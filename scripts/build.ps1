param([string]$Python = 'python', [switch]$PackageOnly)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root 'build\bin'
$dist = Join-Path $root 'dist'
$licenses = Join-Path $root 'build\licenses'
$tools = @{
    'battery-check' = 'battery_check'
    'network-check' = 'network_check'
    'obs-preflight' = 'obs_preflight'
    'obs-transfer' = 'obs_transfer'
    'usb-check' = 'usb_check'
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null
New-Item -ItemType Directory -Force -Path $licenses | Out-Null
$pythonLicense = & $Python -c 'import sys; from pathlib import Path; print(Path(sys.base_prefix) / "LICENSE.txt")'
if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $pythonLicense)) { throw 'CPython license file not found.' }
Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenses 'CPython.txt')
$socketLicense = & $Python -c 'from importlib.metadata import distribution; d=distribution("websocket-client"); print(next(d.locate_file(f) for f in d.files if f.name == "LICENSE"))'
if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $socketLicense)) { throw 'websocket-client license file not found.' }
Copy-Item -LiteralPath $socketLicense -Destination (Join-Path $licenses 'websocket-client.txt')
foreach ($name in ($tools.Keys | Sort-Object)) {
    $entry = Join-Path $root ('src\fieldkit\' + $tools[$name] + '.py')
    $buildArgs = @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--noupx', '--log-level', 'WARN',
        '--name', $name, '--paths', (Join-Path $root 'src'), '--distpath', $bin,
        '--workpath', (Join-Path $root 'build\cache'), '--specpath', (Join-Path $root 'build\spec'), $entry)
    if (!$PackageOnly) {
        & $Python @buildArgs
        if ($LASTEXITCODE -ne 0) { throw "Build failed: $name" }
    }
    $exe = Join-Path $bin ($name + '.exe')
    & $exe --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "CLI smoke test failed: $name" }
    $archive = Join-Path $dist ($name + '-windows-x64.zip')
    Compress-Archive -LiteralPath @($exe, (Join-Path $root 'README.md'), (Join-Path $root 'THIRD_PARTY.md'), $licenses) -DestinationPath $archive -Force
}

$checksums = Get-ChildItem -LiteralPath $dist -Filter '*-windows-x64.zip' | Sort-Object Name | ForEach-Object {
    $hash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
    "$($hash.Hash.ToLowerInvariant())  $($_.Name)"
}
$checksums | Set-Content -LiteralPath (Join-Path $dist 'SHA256SUMS.txt') -Encoding ascii
