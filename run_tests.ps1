# -Fast: daily fast set (tests.fast_suite, under one minute).
# -Full: full offline suite (tools/verify_m3_offline.py); required before a PR.
# No switch: the V1 retrieval / routing evaluations below (they need Ollama).
param(
    [switch]$Fast,
    [switch]$Full,
    [string]$Python = (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")
)

$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8

if ($Fast -and $Full) { throw "Use either -Fast or -Full, not both." }
if ($Fast -or $Full) {
    # unittest reports on stderr; do not turn that into a terminating error.
    $ErrorActionPreference = "Continue"
    $names = "AFTERSALES_DECISION_POLICY", "AFTERSALES_KB_OFFLINE", "AFTERSALES_KB_EMBED_CACHE"
    $saved = @{}
    foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
    Push-Location $PSScriptRoot
    try {
        if ($Fast) {
            # The same model-free settings tools/verify_m3_offline.py uses for the full suite.
            Remove-Item Env:AFTERSALES_DECISION_POLICY -ErrorAction SilentlyContinue
            $env:AFTERSALES_KB_OFFLINE = "1"
            $env:AFTERSALES_KB_EMBED_CACHE = Join-Path $PSScriptRoot ".cache\m3-embeddings"
            & $Python -X utf8 -m unittest tests.fast_suite
        } else {
            & $Python -X utf8 tools\verify_m3_offline.py --output tmp\full-offline-tests.json
        }
        $code = $LASTEXITCODE
    } finally {
        foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
        Pop-Location
    }
    exit $code
}

Write-Host "`n=== 1/4 基础检索测试 ===" -ForegroundColor Cyan
& $Python "$PSScriptRoot\evaluate.py"

Write-Host "`n=== 2/4 扩展检索测试 ===" -ForegroundColor Cyan
& $Python "$PSScriptRoot\evaluate.py" "$PSScriptRoot\eval_cases_extended.json"

Write-Host "`n=== 3/4 Agent 路由测试 ===" -ForegroundColor Cyan
& $Python "$PSScriptRoot\evaluate_routes.py"

Write-Host "`n=== 4/4 无答案拒答测试（耗时较长） ===" -ForegroundColor Cyan
& $Python "$PSScriptRoot\evaluate_no_answer.py"
