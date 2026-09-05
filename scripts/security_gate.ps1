<#
.SYNOPSIS
    Mandatory pre-deploy security gate for Azure Support Agent.

.DESCRIPTION
    Executes the automatable portion of docs/improvement-plans/security-hardening/
    start to finish, and enforces a staleness check on the parts that cannot be
    automated (manual pen-test phases).

    DESIGN PRINCIPLE — FAIL CLOSED.
    A missing tool is a FINDING, not a skip. A gate that silently skips is worse than no
    gate at all, because it manufactures confidence without providing assurance.

.PARAMETER Stage
    preflight  - run before anything is published (Phase S, blocks Phases 1-4)
    image      - scan a built image for baked-in secrets (after Phase 2)
    postdeploy - assert production runtime config (after Phase 5)
    all        - preflight only; image/postdeploy need their own inputs

.PARAMETER ImageRef
    Image to scan when -Stage image.

.PARAMETER BaseUrl
    Live FQDN when -Stage postdeploy.

.PARAMETER AppName
.PARAMETER ResourceGroup
    Container App and resource group to assert against when -Stage postdeploy.
    Deployment-specific, so they are parameters rather than baked-in values -- this
    script is committed to a public repository and is meant to be reusable.
    Defaults come from the AZSUP_APP_NAME / AZSUP_RESOURCE_GROUP environment variables.

.PARAMETER Quick
    Skip the slow full-history secret scan. ONLY for iterating on the gate itself.
    Never use for a real deploy.

.OUTPUTS
    Findings table to stdout + JSON report at
    docs/improvement-plans/security-hardening/_state/gate-report.json

    Exit 0 = clean, no findings
    Exit 1 = findings present - agent MUST present them and obtain explicit
             user confirmation before proceeding
    Exit 2 = the gate could not run correctly - always blocking
#>
[CmdletBinding()]
param(
    [ValidateSet('preflight', 'image', 'postdeploy', 'all')]
    [string]$Stage = 'preflight',
    [string]$ImageRef = '',
    [string]$BaseUrl = '',
    [string]$AppName = $env:AZSUP_APP_NAME,
    [string]$ResourceGroup = $env:AZSUP_RESOURCE_GROUP,
    [switch]$Quick
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
# Reuse the pre-existing .security/ convention established by the 2026-06-27 full scan.
# It is already gitignored (.gitignore:117) and already holds the raw tool outputs.
$stateDir = Join-Path $repo '.security\reports'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

$script:Findings = @()
$script:Checks = @()
# Ledger entries that THIS run satisfies. Without this, suites the gate itself
# executes every deploy would still report as stale forever - self-inflicted noise,
# and noise is what gets gates switched off.
$script:LedgerStamp = @{}

function Add-Finding {
    param(
        [Parameter(Mandatory)][string]$Id,
        [Parameter(Mandatory)][ValidateSet('BLOCKER', 'HIGH', 'MEDIUM', 'LOW', 'INFO')][string]$Severity,
        [Parameter(Mandatory)][string]$Title,
        [string]$Detail = '',
        [string]$Action = ''
    )
    $script:Findings += [pscustomobject]@{
        Id = $Id; Severity = $Severity; Title = $Title; Detail = $Detail; Action = $Action
    }
}

function Add-Check {
    param([string]$Name, [string]$Result, [string]$Note = '')
    $script:Checks += [pscustomobject]@{ Check = $Name; Result = $Result; Note = $Note }
    $colour = switch ($Result) { 'PASS' { 'Green' } 'FAIL' { 'Red' } 'ERROR' { 'Red' } default { 'Yellow' } }
    Write-Host ("  {0,-42} {1}{2}" -f $Name, $Result, $(if ($Note) { "  ($Note)" } else { '' })) -ForegroundColor $colour
}

function Test-Tool { param([string]$Name) return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

$dockerOk = $false
if (Test-Tool docker) {
    $dockerServer = docker version --format '{{.Server.Version}}' 2>$null
    $dockerOk = $LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($dockerServer -join ''))
}
$py = Join-Path $repo 'backend\.venv\Scripts\python.exe'

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " PRE-DEPLOY SECURITY GATE - stage: $Stage" -ForegroundColor Cyan
Write-Host " plan: docs/improvement-plans/security-hardening/" -ForegroundColor Cyan
Write-Host "==============================================================" -ForegroundColor Cyan

# =====================================================================
# STAGE: PREFLIGHT
# =====================================================================
if ($Stage -in 'preflight', 'all') {

    # ---- S1  Working tree / diff hygiene -----------------------------
    Write-Host "`n[S1] Working tree & diff hygiene" -ForegroundColor White
    Push-Location $repo

    $dirty = git status --porcelain
    if ($dirty) {
        Add-Check 'working tree clean' 'WARN' "$(($dirty | Measure-Object).Count) uncommitted"
    } else { Add-Check 'working tree clean' 'PASS' }

    # Sensitive paths anywhere in tracked files.
    # .env.example / .sample / .template are INTENTIONALLY tracked - they are the
    # documented placeholder templates. Excluding them is deliberate, not an oversight.
    $sensitive = git ls-files | Where-Object {
        $_ -notmatch '\.(example|sample|template)$' -and (
            $_ -match '(^|/)\.env($|\.)' -or
            $_ -match '(^|/)\.data/' -or
            $_ -match '\.(key|pem|pfx|p12|jks|keystore)$' -or
            $_ -match '(^|/)\.(pgpass|pgname|pgloc|acrname|stgname|imgdigest)$' -or
            $_ -match '(^|/)secret\.key$'
        )
    }
    if ($sensitive) {
        Add-Check 'no sensitive files tracked' 'FAIL' "$(($sensitive | Measure-Object).Count) file(s)"
        Add-Finding -Id 'S1.1' -Severity 'BLOCKER' -Title 'Sensitive file is tracked by git' `
            -Detail ($sensitive -join ', ') `
            -Action 'git rm --cached the file, add to .gitignore, and rotate anything it contained.'
    } else { Add-Check 'no sensitive files tracked' 'PASS' }

    # Live tenant identifiers must not appear in tracked source.
    #
    # The list lives in a GITIGNORED file, not in this script. This script is committed
    # to a PUBLIC repo, so hardcoding the identifiers here would make the detector the
    # leak — it would publish exactly what it exists to protect. Other clones have no
    # such file and the check simply no-ops, which is correct: these identifiers belong
    # to this operator, not to the product.
    $idFile = Join-Path $repo '.security\live-identifiers.txt'
    if (-not (Test-Path $idFile)) {
        Add-Check 'no live tenant identifiers tracked' 'SKIP' 'no .security/live-identifiers.txt'
    }
    else {
        $liveIds = Get-Content $idFile |
            ForEach-Object { ($_ -split '#')[0].Trim() } |
            Where-Object { $_ }

        $identifierGuard = Join-Path $repo 'scripts\check_live_identifiers.py'
        if (-not (Test-Path $identifierGuard) -or -not (Test-Path $py)) {
            Add-Check 'no live tenant identifiers tracked' 'ERROR' 'identifier guard or Python environment missing'
            Add-Finding -Id 'S1.2' -Severity 'BLOCKER' -Title 'Live identifier scan could not run' `
                -Action 'Restore scripts/check_live_identifiers.py and the backend Python environment before publishing.'
        }
        else {
            $guardOutput = & $py $identifierGuard 2>&1
            if ($LASTEXITCODE -ne 0) {
                Add-Check 'no live tenant identifiers tracked' 'FAIL' 'identifier guard rejected publishable content'
                Add-Finding -Id 'S1.2' -Severity 'HIGH' -Title 'Live tenant/connection/app identifier in source' `
                    -Detail (($guardOutput | Select-Object -First 10) -join "`n") `
                    -Action 'Replace with a placeholder or move to an ignored fixture. Not secret, but reconnaissance value on a public repo.'
            }
            else { Add-Check 'no live tenant identifiers tracked' 'PASS' "$($liveIds.Count) identifier(s) checked" }
        }
    }

    Pop-Location

    # ---- S2  Secret scanning (plan 03) -------------------------------
    Write-Host "`n[S2] Secret scanning  (plan 03-secrets-scanning.md)" -ForegroundColor White
    if (-not $dockerOk) {
        Add-Check 'gitleaks' 'ERROR' 'docker unavailable'
        Add-Finding -Id 'S2.0' -Severity 'BLOCKER' -Title 'Secret scanning could not run (docker unavailable)' `
            -Action 'Start Docker Desktop, or install gitleaks natively. Do NOT deploy without a secret scan.'
    }
    else {
        $repoMount = "$($repo):/repo"
        $glArgs = @('detect', '--source=/repo', '--redact', '--no-banner',
                    '--config=/repo/.gitleaks.toml',
                    '--report-format=json', '--report-path=/repo/.security/reports/gitleaks-gate.json')
        if ($Quick) { $glArgs += '--no-git' }   # filesystem only, skips history
        docker run --rm -v $repoMount zricethezav/gitleaks:latest @glArgs 2>&1 | Out-Null
        $glExit = $LASTEXITCODE
        $glReport = Join-Path $stateDir 'gitleaks-gate.json'

        if ($glExit -eq 0) {
            Add-Check ("gitleaks{0}" -f $(if ($Quick) { ' (QUICK - no history)' } else { ' (full history)' })) 'PASS'
            if ($Quick) {
                Add-Finding -Id 'S2.2' -Severity 'MEDIUM' -Title 'Secret scan ran in QUICK mode - git history NOT scanned' `
                    -Action 'Re-run without -Quick before a real deploy.'
            } else {
                # A clean FULL-history scan IS the 'secrets-full-history' suite.
                $script:LedgerStamp['secrets-full-history (03)'] = (Get-Date).ToString('yyyy-MM-dd')
            }
        }
        elseif (Test-Path $glReport) {
            $leaks = @(Get-Content $glReport -Raw | ConvertFrom-Json)
            Add-Check 'gitleaks' 'FAIL' "$($leaks.Count) finding(s)"
            $summary = ($leaks | Select-Object -First 10 | ForEach-Object {
                "$($_.RuleID) @ $($_.File):$($_.StartLine) (commit $($_.Commit))"
            }) -join "`n"
            Add-Finding -Id 'S2.1' -Severity 'BLOCKER' -Title "gitleaks found $($leaks.Count) potential secret(s)" `
                -Detail $summary `
                -Action 'Triage each. If real: ROTATE FIRST, then purge from history. Redacted report: .security/reports/gitleaks-gate.json. If a verified FP, add it to .gitleaks.toml with a justifying comment.'
        }
        else {
            Add-Check 'gitleaks' 'ERROR' "exit $glExit, no report"
            Add-Finding -Id 'S2.0' -Severity 'BLOCKER' -Title 'gitleaks did not produce a report' `
                -Action 'Investigate before deploying.'
        }
    }

    # Pre-commit hook present? (plan 03 layer 1)
    if (Test-Path (Join-Path $repo '.pre-commit-config.yaml')) {
        Add-Check 'pre-commit hook configured' 'PASS'
    } else {
        Add-Check 'pre-commit hook configured' 'FAIL' 'missing'
        Add-Finding -Id 'S2.3' -Severity 'HIGH' -Title 'No .pre-commit-config.yaml - secrets are not blocked locally' `
            -Action 'Plan 03, Layer 1. Blocks secrets locally, before they can reach git history.'
    }

    # ---- S3  CI existence (plan 12) ----------------------------------
    Write-Host "`n[S3] CI security pipeline  (plan 12-ci-pipeline.md)" -ForegroundColor White
    if (Test-Path (Join-Path $repo '.github\workflows')) {
        $wf = Get-ChildItem (Join-Path $repo '.github\workflows') -Filter *.yml -ErrorAction SilentlyContinue
        Add-Check 'CI workflows present' 'PASS' "$($wf.Count) workflow(s)"
    } else {
        Add-Check 'CI workflows present' 'FAIL' 'no .github/ at all'
        Add-Finding -Id 'S3.1' -Severity 'HIGH' -Title 'No CI: nothing scans automatically on push' `
            -Detail 'No .github/ directory exists. Every check is manual and therefore skippable.' `
            -Action 'Plan 12. Without CI, every check is manual and therefore skippable.'
    }

    # GitHub push protection
    if (Test-Tool gh) {
        $ss = gh api repos/zmustafa/AzureSupportAgent --jq '.security_and_analysis.secret_scanning_push_protection.status' 2>$null
        if ($ss -eq 'enabled') { Add-Check 'GitHub push protection' 'PASS' }
        else {
            Add-Check 'GitHub push protection' 'FAIL' "status=$ss"
            Add-Finding -Id 'S3.2' -Severity 'HIGH' -Title 'GitHub secret-scanning push protection is not enabled' `
                -Action 'Repo Settings > Code security > Push protection. Server-side backstop if local hooks are bypassed.'
        }
    } else { Add-Check 'GitHub push protection' 'WARN' 'gh unavailable' }

    # ---- S4  Dependency audit (plan 04) ------------------------------
    Write-Host "`n[S4] Dependency vulnerabilities  (plan 04-supply-chain.md)" -ForegroundColor White
    $pipAudit = & $py -m pip_audit --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Add-Check 'pip-audit' 'ERROR' 'not installed'
        Add-Finding -Id 'S4.0' -Severity 'MEDIUM' -Title 'pip-audit not installed - Python deps unscanned' `
            -Action "backend\.venv\Scripts\python.exe -m pip install pip-audit"
    }
    else {
        $pa = & $py -m pip_audit -r (Join-Path $repo 'backend\requirements.txt') --format json 2>$null
        if ($LASTEXITCODE -eq 0) { Add-Check 'pip-audit (backend)' 'PASS' }
        else {
            $vulns = try { ($pa | ConvertFrom-Json).dependencies | Where-Object { $_.vulns } } catch { $null }
            $n = @($vulns).Count
            Add-Check 'pip-audit (backend)' 'FAIL' "$n vulnerable package(s)"
            Add-Finding -Id 'S4.1' -Severity 'HIGH' -Title "pip-audit: $n vulnerable Python package(s)" `
                -Detail (($vulns | Select-Object -First 10 | ForEach-Object { "$($_.name) $($_.version) -> $($_.vulns.id -join ',')" }) -join "`n") `
                -Action 'Upgrade, or record a disposition with an expiry per plan 04.3.'
        }
    }

    Push-Location (Join-Path $repo 'frontend')
    npm audit --audit-level=high 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Add-Check 'npm audit (frontend, high+)' 'PASS' }
    else {
        $na = npm audit --json 2>$null | ConvertFrom-Json
        $hi = $na.metadata.vulnerabilities.high + $na.metadata.vulnerabilities.critical
        Add-Check 'npm audit (frontend, high+)' 'FAIL' "$hi high/critical"
        Add-Finding -Id 'S4.2' -Severity 'HIGH' -Title "npm audit: $hi high/critical frontend vulnerabilities" `
            -Action 'npm audit fix, or record a disposition per plan 04.3.'
    }
    Pop-Location

    # ---- S5  SAST (plan 05) ------------------------------------------
    Write-Host "`n[S5] Static analysis  (plan 05-sast.md)" -ForegroundColor White
    # Run with the PROJECT config so backend/pyproject.toml is the single source of
    # truth for which security rules apply and which are ignored-with-justification.
    # Passing `--select S,ASYNC` on the CLI would override the config's `ignore` list
    # too, resurfacing all 154 reviewed findings every run.
    # Filter to security codes so ordinary lint (unused imports, etc.) is not reported
    # as a security finding.
    $ruff = & $py -m ruff check (Join-Path $repo 'backend\app') --output-format json 2>$null
    $ruffExit = $LASTEXITCODE
    $ruffAll = @()
    $ruffParsed = $false
    try {
        # Keep an empty JSON array as an array: pipeline enumeration otherwise turns
        # a clean [] response into $null and incorrectly labels the scan an error.
        $ruffResult = ConvertFrom-Json -InputObject ($ruff -join "`n") -NoEnumerate -ErrorAction Stop
        if ($ruffResult -isnot [Array]) { throw 'Expected a Ruff JSON array' }
        $ruffAll = @($ruffResult)
        $ruffParsed = $ruffExit -in @(0, 1)
    } catch { $ruffParsed = $false }
    $ruffSec = @($ruffAll | Where-Object { $_.code -match '^(S|ASYNC)\d+$' })
    if (-not $ruffParsed) {
        Add-Check 'ruff security rules (S,ASYNC)' 'ERROR' 'scan failed or invalid JSON'
        Add-Finding -Id 'S5.0-ruff' -Severity 'BLOCKER' -Title 'Ruff security scan could not be verified' `
            -Action 'Resolve the scanner execution or output error before publishing.'
    }
    elseif ($ruffSec.Count -eq 0) { Add-Check 'ruff security rules (S,ASYNC)' 'PASS' "$($ruffAll.Count) non-security lint" }
    else {
        Add-Check 'ruff security rules (S,ASYNC)' 'WARN' "$($ruffSec.Count) finding(s)"
        Add-Finding -Id 'S5.1' -Severity 'MEDIUM' -Title "ruff security rules: $($ruffSec.Count) NEW finding(s)" `
            -Detail (($ruffSec | Select-Object -First 8 | ForEach-Object { "$($_.code) $($_.filename):$($_.location.row)" }) -join "`n") `
            -Action 'These are NOT in the reviewed baseline in backend/pyproject.toml. Fix, or add an ignore WITH a written justification.'
    }

    $bandit = & $py -m bandit -r (Join-Path $repo 'backend\app') -ll -f json -q 2>$null
    $banditExit = $LASTEXITCODE
    $banditN = -1
    try {
        if ($banditExit -notin @(0, 1)) { throw 'Bandit execution failed' }
        $banditReport = ConvertFrom-Json -InputObject ($bandit -join "`n") -ErrorAction Stop
        if ($banditReport.PSObject.Properties.Name -notcontains 'results' -or
            $banditReport.PSObject.Properties.Name -notcontains 'metrics' -or
            @($banditReport.errors).Count -gt 0) { throw 'Incomplete Bandit report' }
        $banditN = @($banditReport.results).Count
    } catch { $banditN = -1 }
    if ($banditN -eq 0) { Add-Check 'bandit (medium+)' 'PASS' }
    elseif ($banditN -lt 0) {
        Add-Check 'bandit (medium+)' 'ERROR' 'could not parse'
        Add-Finding -Id 'S5.0-bandit' -Severity 'BLOCKER' -Title 'Bandit security scan could not be verified' `
            -Action 'Resolve the scanner execution or output error before publishing.'
    }
    else {
        Add-Check 'bandit (medium+)' 'WARN' "$banditN finding(s)"
        Add-Finding -Id 'S5.2' -Severity 'MEDIUM' -Title "bandit: $banditN medium+ finding(s)" `
            -Action 'Triage; suppress with a justification comment or fix.'
    }

    # ---- S6  Security regression tests (plan 07/08/09) ---------------
    Write-Host "`n[S6] Security regression tests" -ForegroundColor White
    Push-Location (Join-Path $repo 'backend')
    $secTests = @(
        'tests/test_security_e2e.py', 'tests/test_rbac.py', 'tests/test_noaccess_role.py',
        'tests/test_tenant_identifier_leaks.py', 'tests/test_permissions_catalog.py'
    ) | Where-Object { Test-Path $_ }
    # Pick up anything matching the security-fix naming convention
    $secTests += (Get-ChildItem tests -Filter 'test_security_fixes_v*.py' -ErrorAction SilentlyContinue |
                  ForEach-Object { "tests/$($_.Name)" })
    # And the new gate-mandated suites once they exist
    $secTests += @('tests/test_route_authz_matrix.py', 'tests/test_prompt_injection_corpus.py',
                   'tests/test_command_runner_fuzz.py') | Where-Object { Test-Path $_ }

    $out = & .venv\Scripts\python.exe -m pytest -q -p no:randomly @secTests 2>&1
    if ($LASTEXITCODE -eq 0) {
        $line = ($out | Select-String -Pattern '\d+ passed' | Select-Object -Last 1).ToString().Trim()
        Add-Check 'security regression suite' 'PASS' $line
    } else {
        Add-Check 'security regression suite' 'FAIL' 'see output'
        Add-Finding -Id 'S6.1' -Severity 'BLOCKER' -Title 'Security regression tests are failing' `
            -Detail (($out | Select-Object -Last 15) -join "`n") `
            -Action 'A failing security test means a previously-fixed vulnerability may have regressed. Do not deploy.'
    }

    # Coverage gaps the plan mandates
    foreach ($t in @(
        @{ f = 'tests/test_route_authz_matrix.py';     s = 'HIGH';   p = '07.1 - every route x 5 principals' },
        @{ f = 'tests/test_prompt_injection_corpus.py'; s = 'HIGH';   p = '09 - indirect prompt injection' },
        @{ f = 'tests/test_command_runner_fuzz.py';    s = 'MEDIUM'; p = '08.1 - validate_command fuzzing' }
    )) {
        if (-not (Test-Path $t.f)) {
            Add-Finding -Id "S6.$($t.p.Split('.')[0])" -Severity $t.s `
                -Title "Missing mandated security test: $($t.f)" `
                -Detail "Plan section $($t.p)" -Action 'Implement before this gate can be considered complete.'
        }
    }
    Pop-Location

    # ---- S7  Container posture (plan 10) -----------------------------
    Write-Host "`n[S7] Container posture  (plan 10-container-infra-iac.md)" -ForegroundColor White
    $dockerfiles = Get-ChildItem $repo -Filter Dockerfile -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                   Where-Object { $_.FullName -notmatch 'node_modules' }
    $rootful = @()
    foreach ($df in $dockerfiles) {
        if (-not (Select-String -Path $df.FullName -Pattern '^\s*USER\s+' -Quiet)) {
            $rootful += $df.FullName.Replace("$repo\", '')
        }
    }
    if ($rootful) {
        Add-Check 'containers run as non-root' 'FAIL' "$($rootful.Count) rootful"
        Add-Finding -Id 'S7.1' -Severity 'HIGH' -Title 'Container image runs as root' `
            -Detail ($rootful -join ', ') -Action 'Plan 10.2. Add a USER directive; verify the /app/.data mount stays writable.'
    } else { Add-Check 'containers run as non-root' 'PASS' }

    $unpinned = @()
    foreach ($df in $dockerfiles) {
        # A digest pin looks like `FROM image:tag@sha256:<hex>`. Note the digest itself
        # contains a colon, so a naive `FROM \S+:\S+$` regex matches PINNED lines too --
        # exclude anything carrying @sha256: before testing for a bare tag.
        $unpinned += (Get-Content $df.FullName | Select-String -Pattern '^\s*FROM\s+' |
            Where-Object { $_.Line -notmatch '@sha256:' -and $_.Line -match '^\s*FROM\s+\S+:\S+' } |
            ForEach-Object { "$($df.Name):$($_.LineNumber) $($_.Line.Trim())" })
    }
    if ($unpinned) {
        Add-Check 'base images digest-pinned' 'WARN' "$($unpinned.Count) unpinned"
        Add-Finding -Id 'S7.2' -Severity 'MEDIUM' -Title 'Base images are tag-pinned, not digest-pinned' `
            -Detail ($unpinned -join "`n") -Action 'Plan 04.4. Pair with an automated digest-bump job.'
    } else { Add-Check 'base images digest-pinned' 'PASS' }

    # ---- S8  Deep-suite staleness (the manual phases) ----------------
    Write-Host "`n[S8] Manual pen-test suite freshness  (plan 13)" -ForegroundColor White
    $ledgerPath = Join-Path $repo '.security\deep-suite-ledger.json'
    # Seeded from .security/SECURITY_SCAN_REPORT.md (2026-06-27 full scan).
    # Suites that scan actually covered carry that date. Suites it did NOT cover
    # stay null so the gate keeps flagging them - do not backfill them to silence noise.
    $defaultLedger = [ordered]@{
        'secrets-full-history (03)'    = @{ last = '2026-06-27'; maxAgeDays = 30 }
        'sast-semgrep (05.3)'          = @{ last = '2026-06-27'; maxAgeDays = 60 }
        'dast-zap-baseline (06.2)'     = @{ last = '2026-06-27'; maxAgeDays = 30 }
        'container-cve (10.1)'         = @{ last = '2026-06-27'; maxAgeDays = 30 }
        'iac-checkov (10.6)'           = @{ last = '2026-06-27'; maxAgeDays = 60 }
        'authn-review (07)'            = @{ last = '2026-06-27'; maxAgeDays = 90 }
        # NOT covered by the 2026-06-27 scan - genuinely never run:
        'bola-matrix (07.3)'           = @{ last = $null; maxAgeDays = 30 }
        'dast-schemathesis (06.1)'     = @{ last = $null; maxAgeDays = 30 }
        'ssrf-sweep (08.3)'            = @{ last = $null; maxAgeDays = 60 }
        'prompt-injection (09.1)'      = @{ last = $null; maxAgeDays = 30 }
        'graph-permission-review (11)' = @{ last = $null; maxAgeDays = 90 }
    }
    if (Test-Path $ledgerPath) {
        $ledger = Get-Content $ledgerPath -Raw | ConvertFrom-Json
    } else {
        $ledger = [pscustomobject]$defaultLedger
    }
    # Apply anything this run satisfied, then persist.
    foreach ($k in $script:LedgerStamp.Keys) {
        if ($ledger.PSObject.Properties.Name -contains $k) { $ledger.$k.last = $script:LedgerStamp[$k] }
    }
    $ledger | ConvertTo-Json -Depth 5 | Set-Content $ledgerPath
    foreach ($p in $ledger.PSObject.Properties) {
        $last = $p.Value.last
        $max = $p.Value.maxAgeDays
        if (-not $last) {
            Add-Check "deep: $($p.Name)" 'FAIL' 'never run'
            Add-Finding -Id "S8.$($p.Name)" -Severity 'HIGH' -Title "Manual suite never executed: $($p.Name)" `
                -Action "Run it per the plan, then set its date in .security/deep-suite-ledger.json"
        } else {
            $age = ([datetime]::UtcNow - [datetime]$last).Days
            if ($age -gt $max) {
                Add-Check "deep: $($p.Name)" 'FAIL' "${age}d old (max ${max}d)"
                Add-Finding -Id "S8.$($p.Name)" -Severity 'MEDIUM' -Title "Manual suite stale: $($p.Name) ($age days old, max $max)" `
                    -Action "Re-run and update .security/deep-suite-ledger.json"
            } else { Add-Check "deep: $($p.Name)" 'PASS' "${age}d old" }
        }
    }
}

# =====================================================================
# STAGE: IMAGE  (after Phase 2 build, before Phase 4 roll)
# =====================================================================
if ($Stage -eq 'image') {
    Write-Host "`n[S9] Built-image scan  (plan 10.1)" -ForegroundColor White
    if (-not $ImageRef) { throw "-ImageRef is required for -Stage image" }
    if (-not $dockerOk) {
        Add-Finding -Id 'S9.0' -Severity 'BLOCKER' -Title 'Cannot scan image (docker unavailable)' -Action 'Start Docker.'
    }
    else {
        docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:latest `
            image --scanners secret --exit-code 1 --quiet $ImageRef 2>&1 | Tee-Object -Variable secOut | Out-Null
        if ($LASTEXITCODE -eq 0) { Add-Check 'image secret scan' 'PASS' }        else {
            Add-Check 'image secret scan' 'FAIL'
            Add-Finding -Id 'S9.1' -Severity 'BLOCKER' -Title 'Secrets baked into the container image' `
                -Detail (($secOut | Select-Object -First 25) -join "`n") `
                -Action 'A deleted file still lives in its original layer. Rebuild, and delete the contaminated published tags.'
        }

        docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:latest `
            image --severity CRITICAL --quiet --format json $ImageRef 2>$null |
            Set-Variable trivyJson
        $trivyExit = $LASTEXITCODE
        $crit = -1
        try {
            if ($trivyExit -ne 0) { throw 'Trivy execution failed' }
            $trivyReport = ConvertFrom-Json -InputObject ($trivyJson -join "`n") -ErrorAction Stop
            if (-not $trivyReport.SchemaVersion -or -not $trivyReport.ArtifactName) {
                throw 'Trivy did not return an image report'
            }
            $crit = @($trivyReport.Results.Vulnerabilities | Where-Object { $_.FixedVersion }).Count
        } catch { $crit = -1 }
        if ($crit -eq 0) {
            Add-Check 'image CRITICAL CVEs with fixes' 'PASS'
            # A clean image CVE scan IS the 'container-cve' suite.
            $lp = Join-Path $repo '.security\deep-suite-ledger.json'
            if (Test-Path $lp) {
                $lg = Get-Content $lp -Raw | ConvertFrom-Json
                if ($lg.PSObject.Properties.Name -contains 'container-cve (10.1)') {
                    $lg.'container-cve (10.1)'.last = (Get-Date).ToString('yyyy-MM-dd')
                    $lg | ConvertTo-Json -Depth 5 | Set-Content $lp
                }
            }
        }
        elseif ($crit -lt 0) {
            Add-Check 'image CRITICAL CVEs with fixes' 'ERROR'
            Add-Finding -Id 'S9.0-cve' -Severity 'BLOCKER' -Title 'Image vulnerability scan could not be verified' `
                -Action 'Resolve the Trivy execution or report error before publishing the image.'
        }
        else {
            Add-Check 'image CRITICAL CVEs with fixes' 'FAIL' "$crit fixable"
            Add-Finding -Id 'S9.2' -Severity 'HIGH' -Title "$crit CRITICAL CVE(s) with an available upstream fix" `
                -Action 'Fixable CVEs are not acceptable to ship. Rebuild with updated packages.'
        }
    }
}

# =====================================================================
# STAGE: POSTDEPLOY  (after Phase 5)
# =====================================================================
if ($Stage -eq 'postdeploy') {
    Write-Host "`n[S10] Production runtime assertions  (plan 10.4)" -ForegroundColor White
    if (-not $BaseUrl) { throw "-BaseUrl is required for -Stage postdeploy" }
    if (-not $AppName -or -not $ResourceGroup) {
        throw "-AppName and -ResourceGroup are required for -Stage postdeploy (or set AZSUP_APP_NAME / AZSUP_RESOURCE_GROUP)"
    }

    $env0 = az containerapp show -n $AppName -g $ResourceGroup `
        --query "properties.template.containers[0].env[].{n:name,v:value}" -o json 2>$null | ConvertFrom-Json
    $get = { param($n) ($env0 | Where-Object { $_.n -eq $n }).v }

    if ((& $get 'ENVIRONMENT') -eq 'production') { Add-Check 'ENVIRONMENT=production' 'PASS' }
    else { Add-Check 'ENVIRONMENT=production' 'FAIL'
           Add-Finding -Id 'S10.1' -Severity 'BLOCKER' -Title 'ENVIRONMENT is not production' `
             -Action 'Non-production mode exposes /docs and relaxes cookie handling.' }

    if ((& $get 'COOKIE_SECURE') -match 'true|1') { Add-Check 'COOKIE_SECURE=true' 'PASS' }
    else { Add-Check 'COOKIE_SECURE=true' 'FAIL'
           Add-Finding -Id 'S10.2' -Severity 'BLOCKER' -Title 'COOKIE_SECURE is not true' `
             -Action 'Session cookie can be sent over plaintext.' }

    $op = & $get 'OPENAPI_PUBLIC'
    if (-not $op) { Add-Check 'OPENAPI_PUBLIC absent' 'PASS' }
    else { Add-Check 'OPENAPI_PUBLIC absent' 'FAIL' "=$op"
           Add-Finding -Id 'S10.3' -Severity 'HIGH' -Title 'OPENAPI_PUBLIC is set in production' `
             -Action 'Publishes the full API schema to unauthenticated attackers.' }

    foreach ($path in @('/openapi.json', '/docs', '/redoc')) {
        $r = try { Invoke-WebRequest "$BaseUrl$path" -TimeoutSec 30 -SkipHttpErrorCheck } catch { $null }
        if ($r -and $r.StatusCode -eq 200 -and $r.Content -match 'openapi|swagger') {
            Add-Check "$path not exposed" 'FAIL'
            Add-Finding -Id 'S10.4' -Severity 'HIGH' -Title "API schema reachable unauthenticated at $path" `
                -Action 'Confirm ENVIRONMENT/OPENAPI_PUBLIC on the live revision.'
        } else { Add-Check "$path not exposed" 'PASS' }
    }

    $h = try { (Invoke-WebRequest $BaseUrl -TimeoutSec 30 -SkipHttpErrorCheck).Headers } catch { @{} }
    foreach ($hdr in @('Content-Security-Policy', 'Strict-Transport-Security', 'X-Content-Type-Options')) {
        if ($h.ContainsKey($hdr)) { Add-Check "header $hdr" 'PASS' }
        else {
            Add-Check "header $hdr" 'WARN' 'absent'
            Add-Finding -Id "S10.5-$hdr" -Severity 'MEDIUM' -Title "Missing security header: $hdr" `
                -Action 'Plan 06.2. CSP matters here because agent-generated SVG is rendered with dangerouslySetInnerHTML.'
        }
    }
}

# =====================================================================
# DISPOSITIONS
# A finding may be knowingly accepted, but ONLY with an owner, a reason and an
# EXPIRY. Without an expiry a temporary acceptance silently becomes permanent -
# exactly how the rel-18 container CVEs went unrechecked for months.
# An EXPIRED disposition is itself a finding.
# =====================================================================
$dispPath = Join-Path $repo '.security\dispositions.json'
if (Test-Path $dispPath) {
    $disp = Get-Content $dispPath -Raw | ConvertFrom-Json
    $kept = @()
    foreach ($f in $script:Findings) {
        $d = $disp.PSObject.Properties | Where-Object { $_.Name -eq $f.Id } | Select-Object -First 1
        if (-not $d) { $kept += $f; continue }
        $expires = [datetime]$d.Value.expires
        if ((Get-Date) -gt $expires) {
            $kept += $f
            Add-Finding -Id "DISP.$($f.Id)" -Severity 'MEDIUM' `
                -Title "Disposition EXPIRED for $($f.Id) ($($d.Value.disposition))" `
                -Detail "Accepted $($d.Value.recorded) by $($d.Value.owner); expired $($d.Value.expires)." `
                -Action 'Re-triage: fix it, or renew the acceptance with a new expiry and a fresh justification.'
        } else {
            $days = [int]($expires - (Get-Date)).TotalDays
            Write-Host ("  disposition: {0} = {1} (expires in {2}d)" -f $f.Id, $d.Value.disposition, $days) -ForegroundColor DarkGray
        }
    }
    $script:Findings = $kept
}

# =====================================================================
# REPORT
# =====================================================================
$blockers = @($script:Findings | Where-Object Severity -eq 'BLOCKER')
$highs    = @($script:Findings | Where-Object Severity -eq 'HIGH')
$mediums  = @($script:Findings | Where-Object Severity -eq 'MEDIUM')

Write-Host "`n=============================================================="  -ForegroundColor Cyan
Write-Host " FINDINGS" -ForegroundColor Cyan
Write-Host "=============================================================="  -ForegroundColor Cyan

if ($script:Findings.Count -eq 0) {
    Write-Host " No findings. Gate PASSED." -ForegroundColor Green
} else {
    foreach ($sev in @('BLOCKER', 'HIGH', 'MEDIUM', 'LOW', 'INFO')) {
        $g = @($script:Findings | Where-Object Severity -eq $sev)
        if (-not $g) { continue }
        $c = switch ($sev) { 'BLOCKER' { 'Red' } 'HIGH' { 'Magenta' } 'MEDIUM' { 'Yellow' } default { 'Gray' } }
        Write-Host "`n  --- $sev ($($g.Count)) ---" -ForegroundColor $c
        foreach ($f in $g) {
            Write-Host ("  [{0}] {1}" -f $f.Id, $f.Title) -ForegroundColor $c
            if ($f.Detail) { ($f.Detail -split "`n" | Select-Object -First 6) | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray } }
            if ($f.Action) { Write-Host "        -> $($f.Action)" -ForegroundColor DarkCyan }
        }
    }
}

$report = [pscustomobject]@{
    timestamp = (Get-Date).ToUniversalTime().ToString('o')
    stage     = $Stage
    sha       = (git -C $repo rev-parse --short HEAD)
    checks    = $script:Checks
    findings  = $script:Findings
    summary   = @{ blocker = $blockers.Count; high = $highs.Count; medium = $mediums.Count; total = $script:Findings.Count }
}
$report | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $stateDir 'gate-report.json')

Write-Host "`n--------------------------------------------------------------"
Write-Host (" BLOCKER {0}   HIGH {1}   MEDIUM {2}   TOTAL {3}" -f $blockers.Count, $highs.Count, $mediums.Count, $script:Findings.Count)
Write-Host " report: .security/reports/gate-report.json"
Write-Host "--------------------------------------------------------------"

if ($blockers.Count -gt 0) {
    Write-Host "`n GATE RESULT: BLOCKED - do not deploy until BLOCKERs are resolved." -ForegroundColor Red
    exit 1
}
if ($script:Findings.Count -gt 0) {
    Write-Host "`n GATE RESULT: FINDINGS PRESENT - explicit user confirmation required." -ForegroundColor Yellow
    exit 1
}
Write-Host "`n GATE RESULT: CLEAN" -ForegroundColor Green
exit 0
