param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root 'build\bin'
$dist = Join-Path $root 'dist'
$tools = @{
    'battery-check' = 'battery_check'
    'network-check' = 'network_check'
    'obs-preflight' = 'obs_preflight'
    'obs-transfer' = 'obs_transfer'
    'usb-check' = 'usb_check'
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null
foreach ($name in ($tools.Keys | Sort-Object)) {
    $entry = Join-Path $root ('src\fieldkit\' + $tools[$name] + '.py')
    $buildArgs = @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--noupx', '--log-level', 'WARN',
        '--name', $name, '--paths', (Join-Path $root 'src'), '--distpath', $bin,
        '--workpath', (Join-Path $root 'build\cache'), '--specpath', (Join-Path $root 'build\spec'), $entry)
    & $Python @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "Build failed: $name" }
    $exe = Join-Path $bin ($name + '.exe')
    & $exe --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "CLI smoke test failed: $name" }
    $archive = Join-Path $dist ($name + '-windows-x64.zip')
    Compress-Archive -LiteralPath @($exe, (Join-Path $root 'README.md'), (Join-Path $root 'THIRD_PARTY.md')) -DestinationPath $archive -Force
}

$checksums = Get-ChildItem -LiteralPath $dist -Filter '*-windows-x64.zip' | Sort-Object Name | ForEach-Object {
    $hash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
    "$($hash.Hash.ToLowerInvariant())  $($_.Name)"
}
$checksums | Set-Content -LiteralPath (Join-Path $dist 'SHA256SUMS.txt') -Encoding ascii
