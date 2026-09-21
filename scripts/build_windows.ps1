# cloudctl agent 本地构建（Windows）
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
# 可选参数：
#   -SkipVenv     直接用当前 Python 环境
#   -NoConfig     不生成 config.json
# 产物：agent/dist/cloudctl-agent.exe

param(
    [switch]$SkipVenv,
    [switch]$NoConfig,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$agent = Join-Path $root "agent"

Write-Host "[1/5] 工作目录：$agent"
Push-Location $agent

try {
    if (-not $SkipVenv) {
        Write-Host "[2/5] 创建虚拟环境 .venv"
        & $Python -m venv .venv
        $py = Join-Path $agent ".venv\Scripts\python.exe"
    } else {
        Write-Host "[2/5] 跳过虚拟环境"
        $py = $Python
    }

    Write-Host "[3/5] 安装依赖"
    & $py -m pip install --upgrade pip
    & $py -m pip install -r requirements.txt

    if (-not $NoConfig) {
        $cfgPath = Join-Path $agent "config.json"
        if (-not (Test-Path $cfgPath)) {
            Write-Host "[4/5] 生成 config.json（请填写 devsrv_token / gh_rules_repo / upload_repo）"
            Copy-Item (Join-Path $agent "config.example.json") $cfgPath
        } else {
            Write-Host "[4/5] 已存在 config.json，未覆盖"
        }
        Write-Host "      也可运行：python scripts/setup_config.py（交互式向导）"
    }

    Write-Host "[5/5] 打包单文件 exe"
    & $py -m PyInstaller --noconfirm --clean --onefile --name cloudctl-agent `
        --add-data "cloudctl_agent/devstatic;cloudctl_agent/devstatic" `
        --hidden-import websockets --hidden-import pynput `
        --hidden-import pynput.keyboard --hidden-import pynput.mouse `
        --collect-submodules pynput run_agent.py

    $exe = Join-Path $agent "dist\cloudctl-agent.exe"
    if (Test-Path $exe) {
        Write-Host "构建完成：$exe"
    } else {
        Write-Host "构建未产出 exe，请检查上面的报错"
        exit 1
    }
} finally {
    Pop-Location
}
