# PolyBot operator shortcuts (PowerShell). Run from repo root: .\scripts\polybot.ps1 <command>
# Requires: Python 3 on PATH; optional: npm i -g @railway/cli for `railway` commands.

param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "pre", "prelive", "test", "clob", "install", "railway-login", "railway-logs")]
    [string]$Command = "help"
)

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Print-Help {
    Write-Host @"
PolyBot scripts (run from repo root)

  .\scripts\polybot.ps1 pre          # preflight (paper-friendly; reads trading.dry_run in settings.yaml)
  .\scripts\polybot.ps1 prelive        # preflight --require-live (wallet + CLOB API required)
  .\scripts\polybot.ps1 clob         # preflight --check-clob (authenticated CLOB smoke test)
  .\scripts\polybot.ps1 test         # pytest tests/
  .\scripts\polybot.ps1 install      # pip install -r requirements.txt
  .\scripts\polybot.ps1 railway-login
  .\scripts\polybot.ps1 railway-logs

CLI installs (manual):
  pip install -r requirements.txt
  npm i -g @railway/cli    # then: railway login, railway link, railway logs
"@
}

switch ($Command) {
    "help" { Print-Help }
    "pre" { python scripts/preflight.py; exit $LASTEXITCODE }
    "prelive" { python scripts/preflight.py --require-live; exit $LASTEXITCODE }
    "clob" { python scripts/preflight.py --check-clob; exit $LASTEXITCODE }
    "test" { python -m pytest tests/ -q --tb=short; exit $LASTEXITCODE }
    "install" { python -m pip install -r requirements.txt; exit $LASTEXITCODE }
    "railway-login" { railway login; exit $LASTEXITCODE }
    "railway-logs" { railway logs; exit $LASTEXITCODE }
}
