#!/usr/bin/env bash
# llm-wiki-copilot 一键安装（macOS / Linux）
#
# 用法：
#   bash install.sh
#   bash install.sh --yes               # 非交互
#   bash install.sh --with-collectors   # 额外安装采集层
#   bash install.sh --skip-model        # 跳过模型预下载

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YES=""; WITH_COLLECTORS=0; SKIP_MODEL=0

for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES="--yes" ;;
    --with-collectors) WITH_COLLECTORS=1 ;;
    --skip-model) SKIP_MODEL=1 ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done

echo ""
echo "================================================================"
echo "  llm-wiki-copilot 安装程序"
echo "================================================================"
echo ""

# ── 1. 检查 Python ──
echo "[1/5] 检查 Python ..."
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "  ✗ 未找到 Python 3.10+。请先安装后重试。"
  exit 1
fi
echo "  ✓ $("$PY" -c 'import sys; print("Python " + ".".join(map(str, sys.version_info[:3])))')"

# ── 2. 创建虚拟环境 ──
echo "[2/5] 创建虚拟环境 .venv ..."
VENV_PY="$ROOT/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
  echo "  ✓ 已存在，跳过"
else
  "$PY" -m venv "$ROOT/.venv"
  echo "  ✓ 已创建"
fi

# ── 3. 安装依赖 ──
echo "[3/5] 安装依赖（首次约 1-3 分钟）..."
"$VENV_PY" -m pip install --upgrade pip --quiet
if ! "$VENV_PY" -m pip install -r "$ROOT/requirements.txt"; then
  echo "  ✗ 依赖安装失败。请检查网络后手动执行："
  echo "    $VENV_PY -m pip install -r requirements.txt"
  exit 1
fi
echo "  ✓ 依赖安装完成"

# ── 4. 可选组件检查 ──
echo "[4/5] 检查可选组件 ..."
for tool in ffmpeg tesseract; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "  ✓ $tool 已安装"
  else
    echo "  · $tool 未安装（可选：音频处理 / 图片 OCR 需要）"
  fi
done

# ── 5. 初始化向导 ──
echo "[5/5] 初始化配置 ..."
"$VENV_PY" "$ROOT/setup.py" $YES

# ── 可选：预下载向量模型 ──
if [ "$SKIP_MODEL" -eq 0 ]; then
  echo ""
  echo "预下载向量模型（约 241MB，只需一次）..."
  if "$VENV_PY" -c "import sys; sys.path.insert(0, '$ROOT/scripts'); import zvec_config; zvec_config.get_embedder()" 2>/dev/null; then
    echo "  ✓ 模型已缓存"
  else
    echo "  · 跳过（首次检索时自动下载）"
  fi
fi

# ── 可选：采集层 ──
if [ "$WITH_COLLECTORS" -eq 1 ]; then
  echo ""
  echo "================================================================"
  echo "  采集层安装（可选组件）"
  echo "================================================================"
  echo "  注意：采集层的代码不属于本项目，走它们各自的官方安装方式。"
  echo ""
  echo "  ▸ Chubby Skills（B站/公众号/X/小红书等，MIT）"
  echo "      git clone https://github.com/chubbyguan/chubbyskills.git"
  echo "      cd chubbyskills && bash setup.sh video"
  echo "  ▸ OpenCLI（8 平台搜索，Apache-2.0）"
  echo "      npm i -g @jackwener/opencli"
  echo "  ▸ 抖音转录（本项目自带脚本，依赖较重）"
  echo "      见 integrations/douyin/README.md（需 funasr+torch+模型，约 2-4GB）"
  echo ""
  echo "  装完后把路径写进 config.yaml 的 integrations 段。"
fi

echo ""
echo "================================================================"
echo "  ✅ 安装完成"
echo "================================================================"
echo ""
echo "  提醒：安装可自动，授权必须人工 ——"
echo "       平台扫码登录、飞书云端工作流、各类密钥需要你自己完成。"
echo ""
echo "  详细说明：README.md · docs/quickstart.md · integrations/README.md"
echo ""
