param(
    [string]$Harness = $env:FAILURE_MEMORY_HARNESS,
    [string]$Version = $env:FAILURE_MEMORY_VERSION,
    [switch]$RuntimeOnly
)

$ErrorActionPreference = "Stop"

if (-not $Harness) {
    $Harness = "auto"
}
if (-not $Version) {
    $Version = "latest"
}

$repository = if ($env:FAILURE_MEMORY_REPOSITORY) {
    $env:FAILURE_MEMORY_REPOSITORY
} else {
    "CongBao/failure-memory"
}
$baseUrl = if ($env:FAILURE_MEMORY_RELEASE_BASE_URL) {
    $env:FAILURE_MEMORY_RELEASE_BASE_URL
} elseif ($Version -eq "latest") {
    "https://github.com/$repository/releases/latest/download"
} else {
    if ($Version -notmatch '^v\d+\.\d+\.\d+(?:-.+)?$') {
        throw "failure-memory: Version must be latest or a v-prefixed release"
    }
    "https://github.com/$repository/releases/download/$Version"
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
    $executable = Join-Path $temporary "failure-memory.exe"
    if ($Version -ne "latest") {
        $downloadedVersion = ((& $executable version) -split '\s+')[1]
        if ("v$downloadedVersion" -ne $Version) {
            throw "failure-memory: downloaded runtime version does not match $Version"
        }
    }
    if ($RuntimeOnly) {
        & $executable install runtime
    } else {
        & $executable install all --harness $Harness
    }
    if ($LASTEXITCODE -ne 0) {
        throw "failure-memory: installation did not complete"
    }
    $runtimePath = if ($env:FAILURE_MEMORY_RUNTIME_PATH) {
        $env:FAILURE_MEMORY_RUNTIME_PATH
    } else {
        Join-Path $env:LOCALAPPDATA "FailureMemory\bin\failure-memory.exe"
    }
    $runtimeDirectory = Split-Path -Parent $runtimePath
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $userPathEntries = @($userPath -split ";" | Where-Object { $_ })
    if ($runtimeDirectory -notin $userPathEntries) {
        $updatedUserPath = (@($runtimeDirectory) + $userPathEntries) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $updatedUserPath, "User")
    }
    $env:Path = "$runtimeDirectory;$env:Path"
    Write-Output "Installed Failure Memory. Restart any agent application that was already open."
} finally {
    Remove-Item -Recurse -Force $temporary -ErrorAction SilentlyContinue
}
