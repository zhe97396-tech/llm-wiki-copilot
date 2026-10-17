# llm-wiki-copilot 一键安装（Windows）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Yes            # 非交互
#   powershell -ExecutionPolicy Bypass -File install.ps1 -WithCollectors # 含采集层
#
param(
    [switch]$Yes,               # 非交互（全部默认）
    [switch]$WithCollectors,    # 额外安装采集层（Chubby / OpenCLI / 抖音转录）
    [switch]$SkipModel          # 跳过 fastembed 模型预下载
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  llm-wiki-copilot 安装程序" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. 检查 Python ──
Write-Host "[1/5] 检查 Python ..." -ForegroundColor Yellow
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { $pyCmd = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $pyCmd) {
    Write-Host "  ✗ 未找到 Python。请先安装 Python 3.10+ 并加入 PATH。" -ForegroundColor Red
    Write-Host "    https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
$pyExe = $pyCmd.Source
$ver = & $pyExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$ok = & $pyExe -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" ; $verOk = ($LASTEXITCODE -eq 0)
if (-not $verOk) {
    Write-Host "  ✗ Python 版本过低（当前 $ver），需要 3.10+" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ Python $ver" -ForegroundColor Green

# ── 2. 创建虚拟环境 ──
Write-Host "[2/5] 创建虚拟环境 .venv ..." -ForegroundColor Yellow
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    Write-Host "  ✓ 已存在，跳过" -ForegroundColor Green
} else {
    & $pyExe -m venv (Join-Path $root ".venv")
    Write-Host "  ✓ 已创建" -ForegroundColor Green
}

# ── 3. 安装依赖 ──
Write-Host "[3/5] 安装依赖（首次约 1-3 分钟，zvec / fastembed 体积不小）..." -ForegroundColor Yellow
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ✗ 依赖安装失败。请检查网络，或手动执行：" -ForegroundColor Red
    Write-Host "    .venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ 依赖安装完成" -ForegroundColor Green

# ── 4. 环境检查（可选组件）──
Write-Host "[4/5] 检查可选组件 ..." -ForegroundColor Yellow
foreach ($tool in @("ffmpeg", "tesseract")) {
    if (Get-Command $tool -ErrorAction SilentlyContinue) {
        Write-Host "  ✓ $tool 已安装" -ForegroundColor Green
    } else {
        Write-Host "  · $tool 未安装（可选：音频处理 / 图片 OCR 需要）" -ForegroundColor DarkGray
    }
}

# ── 5. 运行初始化向导 ──
Write-Host "[5/5] 初始化配置 ..." -ForegroundColor Yellow
$setupArgs = @((Join-Path $root "setup.py"))
if ($Yes) { $setupArgs += "--yes" }
& $venvPy @setupArgs

# ── 可选：预下载向量模型 ──
if (-not $SkipModel) {
    Write-Host ""
    Write-Host "预下载向量模型（约 241MB，只需一次）..." -ForegroundColor Yellow
    & $venvPy -c "import sys; sys.path.insert(0, r'$root\scripts'); import zvec_config; zvec_config.get_embedder()" 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "  ✓ 模型已缓存" -ForegroundColor Green }
    else { Write-Host "  · 跳过（首次检索时会自动下载）" -ForegroundColor DarkGray }
}

# ── 可选：采集层 ──
if ($WithCollectors) {
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  采集层安装（可选组件）" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  注意：采集层的代码不属于本项目，走它们各自的官方安装方式。" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  ▸ Chubby Skills（B站/公众号/X/小红书等，MIT）" -ForegroundColor Yellow
    Write-Host "      git clone https://github.com/chubbyguan/chubbyskills.git"
    Write-Host "      cd chubbyskills; bash setup.sh video"
    Write-Host "  ▸ OpenCLI（8 平台搜索，Apache-2.0）" -ForegroundColor Yellow
    Write-Host "      npm i -g @jackwener/opencli"
    Write-Host "  ▸ 抖音转录（本项目自带脚本，依赖较重）" -ForegroundColor Yellow
    Write-Host "      见 integrations/douyin/README.md（需 funasr+torch+模型，约 2-4GB）"
    Write-Host ""
    Write-Host "  装完后把路径写进 config.yaml 的 integrations 段。" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  ✅ 安装完成" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  提醒：安装可自动，授权必须人工 ——"
Write-Host "       平台扫码登录、飞书云端工作流、各类密钥需要你自己完成。"
Write-Host ""
Write-Host "  详细说明：README.md · docs/quickstart.md · integrations/README.md"
Write-Host ""
