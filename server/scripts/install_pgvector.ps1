<#
.SYNOPSIS
    Installs the pgvector extension into a local Windows PostgreSQL.

.DESCRIPTION
    pgvector ships source, not Windows binaries: the official install path is
    `nmake` under the Visual Studio x64 Native Tools prompt, which means a
    multi-gigabyte build-tools install for three files. This script instead
    fetches a prebuilt community binary and copies those three things into the
    PostgreSQL directory:

        lib\vector.dll                    the extension itself
        share\extension\vector*           its control file and SQL definitions
        include\server\extension\vector\  headers, for anything compiled against it

    That is all `CREATE EXTENSION vector` needs. Nothing is registered, no
    service is reconfigured, and the running server does not need restarting.

    The binary comes from https://github.com/andreiramani/pgvector_pgsql_windows,
    a third-party build of the pgvector sources. It is not published by the
    pgvector project. The script prints the SHA-256 of what it downloaded so it
    can be recorded and compared on a later run; if that is not a basis you are
    comfortable with, build from source instead and the rest of this project is
    unaffected.

.NOTES
    Must be run as Administrator: C:\Program Files is not user-writable.

.EXAMPLE
    # In an elevated PowerShell, from the repository root:
    powershell -ExecutionPolicy Bypass -File server\scripts\install_pgvector.ps1
#>

[CmdletBinding()]
param(
    # PostgreSQL installation root — the directory containing bin, lib and share.
    [string]$PostgresRoot = "C:\Program Files\PostgreSQL\18",

    # pgvector release to install, and the major PostgreSQL version it was built for.
    [string]$Version = "0.8.6",
    [string]$PgMajor = "18"
)

$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run as Administrator: it writes into $PostgresRoot."
    }
}

Assert-Administrator

if (-not (Test-Path (Join-Path $PostgresRoot "lib") -PathType Container)) {
    throw "No PostgreSQL found at '$PostgresRoot'. Pass -PostgresRoot with the right path."
}

# Refuse to install a build for a different major version. PostgreSQL extension
# modules are ABI-locked to their major version; the wrong one loads and then
# crashes the backend, which is a far worse failure than not installing at all.
$serverExe = Join-Path $PostgresRoot "bin\postgres.exe"
if (Test-Path $serverExe) {
    $reported = & $serverExe --version
    if ($reported -notmatch "\s$PgMajor(\.|\s|$)") {
        throw "Server at '$PostgresRoot' reports '$reported', which is not major version $PgMajor. Pass -PgMajor to match."
    }
    Write-Host "PostgreSQL: $reported"
}

$asset = "vector.v$Version-pg$PgMajor.zip"
$url = "https://github.com/andreiramani/pgvector_pgsql_windows/releases/download/${Version}_$PgMajor/$asset"
$work = Join-Path ([System.IO.Path]::GetTempPath()) "pgvector-$Version-pg$PgMajor"
$zip = Join-Path $work $asset

Write-Host "Downloading $url"
New-Item -ItemType Directory -Force -Path $work | Out-Null
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing

$hash = (Get-FileHash -Path $zip -Algorithm SHA256).Hash
Write-Host "SHA-256: $hash"

$extracted = Join-Path $work "extracted"
if (Test-Path $extracted) { Remove-Item -Recurse -Force $extracted }
Expand-Archive -Path $zip -DestinationPath $extracted -Force

# The archive mirrors the PostgreSQL layout, so each top-level directory in it
# maps onto the same directory under $PostgresRoot.
$copied = 0
foreach ($subdirectory in @("lib", "share", "include")) {
    $from = Join-Path $extracted $subdirectory
    if (-not (Test-Path $from)) { continue }
    $to = Join-Path $PostgresRoot $subdirectory
    Write-Host "Copying $subdirectory\ -> $to"
    Copy-Item -Path (Join-Path $from "*") -Destination $to -Recurse -Force
    $copied++
}
if ($copied -eq 0) { throw "The archive did not contain lib/, share/ or include/ — nothing installed." }

$dll = Join-Path $PostgresRoot "lib\vector.dll"
$control = Join-Path $PostgresRoot "share\extension\vector.control"
foreach ($required in @($dll, $control)) {
    if (-not (Test-Path $required)) { throw "Expected '$required' after the copy, but it is not there." }
}

Write-Host ""
Write-Host "pgvector $Version installed into $PostgresRoot."
Write-Host "  $dll"
Write-Host "  $control"
Write-Host ""
Write-Host "Next, create the database and enable the extension (as a superuser):"
Write-Host "  createdb -U postgres voice_agent_kb"
Write-Host "  psql -U postgres -d voice_agent_kb -c ""CREATE EXTENSION vector;"""
Write-Host ""
Write-Host "Then set KB_DATABASE_URL in server\.env and run:  uv run ingest.py init"
