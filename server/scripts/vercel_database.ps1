# Point the Vercel deployment at the hosted Supabase database and finish the setup.
#
# Run from anywhere:   powershell -File server\scripts\vercel_database.ps1 [-BotUrl https://<ngrok host>]
#
# It asks for the project's database password (typed locally, never pasted into
# a chat or a shell history), then:
#   1. creates the campaign tables on the hosted database (`uv run campaign.py init`),
#   2. sets KB_DATABASE_URL / DATABASE_URL (and APP_BOT_URL, if given) on Vercel production,
#   3. deploys (`vercel --prod`) and checks that the session endpoint answers.
#
# The connection goes through Supabase's *session pooler* on port 5432: the direct host
# (db.<ref>.supabase.co) is IPv6-only and Vercel's functions are IPv4. README,
# "Deploying the application to Vercel", step 1.

param(
    [string]$BotUrl = "",
    [switch]$SkipInit
)

$ErrorActionPreference = "Stop"
$serverDir = Split-Path $PSScriptRoot -Parent
Set-Location $serverDir

$ref = "xyotpizyomvnaalrvvjv"
$poolerHost = "aws-0-ap-southeast-2.pooler.supabase.com"
$site = "https://mubeenkhalid-ai-call-agent.vercel.app"

# `campaign.py` loads .env with override=True: a DATABASE_URL line there would beat
# the one this script sets. KB_DATABASE_URL in .env is fine (DATABASE_URL takes precedence).
if (Test-Path ".env") {
    $clash = Get-Content ".env" | Where-Object { $_ -match "^\s*DATABASE_URL\s*=" }
    if ($clash) {
        throw "server\.env defines DATABASE_URL, which would override this script's value for the init step. Comment it out and rerun."
    }
}

$secure = Read-Host "Database password for Supabase project $ref (Project Settings -> Database)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if (-not $plain) { throw "No password entered." }

$encoded = [uri]::EscapeDataString($plain)
# The laptop initialises through the session pooler (5432); the deployment runs
# through the transaction pooler (6543), the mode meant for serverless functions.
$url = "postgresql://postgres.${ref}:${encoded}@${poolerHost}:5432/postgres?sslmode=require"
$vercelUrl = "postgresql://postgres.${ref}:${encoded}@${poolerHost}:6543/postgres?sslmode=require"

if (-not $SkipInit) {
    Write-Host ""
    Write-Host "1/3  Creating the campaign tables on $poolerHost ..."
    $env:DATABASE_URL = $url
    try {
        uv run campaign.py init
        if ($LASTEXITCODE -ne 0) { throw "campaign.py init failed (exit $LASTEXITCODE). Wrong password, or the project is paused." }
        $env:KB_DATABASE_URL = $url
        uv run ingest.py init
        if ($LASTEXITCODE -ne 0) { throw "ingest.py init failed (exit $LASTEXITCODE)." }
    } finally {
        Remove-Item Env:\DATABASE_URL -ErrorAction SilentlyContinue
        Remove-Item Env:\KB_DATABASE_URL -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "1/3  Skipping the table creation (-SkipInit)."
}

Write-Host ""
Write-Host "2/3  Setting the Vercel production variables ..."
vercel env add KB_DATABASE_URL production --sensitive --force --yes --value $vercelUrl
if ($LASTEXITCODE -ne 0) { throw "vercel env add KB_DATABASE_URL failed." }
vercel env add DATABASE_URL production --sensitive --force --yes --value $vercelUrl
if ($LASTEXITCODE -ne 0) { throw "vercel env add DATABASE_URL failed." }
if ($BotUrl) {
    vercel env add APP_BOT_URL production --sensitive --force --yes --value $BotUrl
    if ($LASTEXITCODE -ne 0) { throw "vercel env add APP_BOT_URL failed." }
}

Write-Host ""
Write-Host "3/3  Deploying ..."
vercel --prod --yes
if ($LASTEXITCODE -ne 0) { throw "vercel --prod failed." }

Write-Host ""
Write-Host "Checking $site/api/app/session ..."
$response = curl.exe -s -w "`n%{http_code}" "$site/api/app/session"
Write-Host $response
if ($response -match "not configured") {
    Write-Host "The deployment still reports a configuration problem; read the detail above." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "Done. Sign in at $site/app/ as admin." -ForegroundColor Green
}

$plain = $null; $url = $null; $vercelUrl = $null; $encoded = $null
