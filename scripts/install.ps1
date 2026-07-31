$ErrorActionPreference = "Stop"

$repository = if ($env:FAILURE_MEMORY_REPOSITORY) {
    $env:FAILURE_MEMORY_REPOSITORY
} else {
    "CongBao/failure-memory"
}
$baseUrl = if ($env:FAILURE_MEMORY_RELEASE_BASE_URL) {
    $env:FAILURE_MEMORY_RELEASE_BASE_URL
} else {
    "https://github.com/$repository/releases/latest/download"
}

$architecture = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
    "X64" { "amd64" }
    "Arm64" { "arm64" }
    default { throw "failure-memory: unsupported CPU architecture" }
}
$archive = "failure-memory_windows_$architecture.zip"
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("failure-memory-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporary | Out-Null

try {
    $archivePath = Join-Path $temporary $archive
    $checksumsPath = Join-Path $temporary "checksums.txt"
    Invoke-WebRequest -Uri "$baseUrl/$archive" -OutFile $archivePath
    Invoke-WebRequest -Uri "$baseUrl/checksums.txt" -OutFile $checksumsPath

    $line = Get-Content $checksumsPath | Where-Object { $_ -match "\s+$([regex]::Escape($archive))$" }
    if (-not $line) {
        throw "failure-memory: release checksum is missing"
    }
    $expected = ($line -split "\s+")[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 $archivePath).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "failure-memory: release checksum verification failed"
    }

    Expand-Archive -Path $archivePath -DestinationPath $temporary
    & (Join-Path $temporary "failure-memory.exe") install runtime
    Write-Host "Installed Failure Memory. Restart any open agent application."
} finally {
    Remove-Item -Recurse -Force $temporary -ErrorAction SilentlyContinue
}
