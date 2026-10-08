# Run once to create PostgreSQL user + database, then create tables.
# Usage (PowerShell):
#   $env:PGPASSWORD = "YOUR_POSTGRES_SUPERUSER_PASSWORD"
#   .\scripts\setup_db.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"

if (-not (Test-Path $Psql)) {
    $found = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $Psql = $found.FullName } else { throw "psql.exe not found. Install PostgreSQL or Docker." }
}

if (-not $env:PGPASSWORD) {
    $secure = Read-Host "Enter PostgreSQL superuser (postgres) password" -AsSecureString
    $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    )
}

Write-Host "Creating role and database for ChairDekho..."
& $Psql -U postgres -h localhost -f "$PSScriptRoot\setup_postgres.sql"
if ($LASTEXITCODE -ne 0) { throw "SQL setup failed" }

Write-Host "Creating tables and seeding admin..."
Push-Location $Root
python "$PSScriptRoot\init_db.py"
Pop-Location

Write-Host "Done. DATABASE_URL=postgresql+asyncpg://chairdekho:chairdekho@localhost:5432/chairdekho"
