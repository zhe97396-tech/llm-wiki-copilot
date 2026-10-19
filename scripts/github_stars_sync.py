#!/usr/bin/env python3
"""GitHub Stars 同步入口（薄壳）—— 引擎是独立包 `ghstars`。

为什么是薄壳：拉取 / 去重 / 分类 / 中文标签 / 检索只有一份实现，命令行、MCP、skill
共用，避免"知识库一份脚本、别处另一份"的漂移。
引擎仓库：https://github.com/zhe97396-tech/ghstars

本文件只做三件事：
1. 把本工程的 config.yaml（integrations.github_stars / llm.*）翻译成 ghstars 的约定
2. 调 `python -m ghstars sync`
3. 有新增时用本工程 venv 跑 `embed.py --incremental` 更新向量索引

用法（与原脚本参数兼容）：
    python scripts/github_stars_sync.py [--dry-run] [--no-llm-tags] [--full] [--limit N]

依赖：`pip install ghstars`（已在 requirements.txt）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import get_env_or_cfg
    from _utils import DATA_ROOT, GITHUB_STARS_DIR
except ImportError as exc:  # 直接拷贝脚本到别处时会缺依赖
    print(f"❌ 无法加载工程模块（{exc}）；请从仓库 scripts/ 目录运行", file=sys.stderr)
    raise SystemExit(2)

INDEX_PATH = DATA_ROOT / ".ghstars" / "index.db"


def _ghstars_env() -> dict:
    """把工程配置翻译成 ghstars 认识的环境变量（ghstars 不读本工程 config）。"""
    env = {
        **os.environ,
        "PYTHONPATH": "",
        "GHSTARS_STORE": str(GITHUB_STARS_DIR),
        "GHSTARS_INDEX": str(INDEX_PATH),
    }
    username = str(
        get_env_or_cfg("GITHUB_USERNAME", "integrations.github_stars.username", "") or ""
    ).strip()
    if username:
        env["GHSTARS_GITHUB_USERNAME"] = username.lstrip("@")

    base = os.environ.get("LLM_API_BASE") or str(get_env_or_cfg("LLM_API_BASE", "llm.base_url", "") or "")
    key = os.environ.get("LLM_API_KEY") or str(get_env_or_cfg("LLM_API_KEY", "llm.api_key", "") or "")
    model = os.environ.get("LLM_MODEL") or str(get_env_or_cfg("LLM_MODEL", "llm.model", "") or "")
    if base:
        env["GHSTARS_LLM_BASE"] = base
    if key:
        env["GHSTARS_LLM_KEY"] = key
    if model:
        env["GHSTARS_LLM_MODEL"] = model
    return env


def _translate_args(argv: list[str]) -> list[str]:
    """旧参数 → ghstars 参数（保持对既有指令文档的兼容）。"""
    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--no-llm-tags":
            out.append("--no-llm")
        elif arg == "--output-dir" and i + 1 < len(argv):
            out.extend(["--dir", argv[i + 1]])
            i += 1
        elif arg.startswith("--output-dir="):
            out.extend(["--dir", arg.split("=", 1)[1]])
        else:
            out.append(arg)
        i += 1
    return out


def _ensure_package() -> bool:
    probe = subprocess.run(
        [sys.executable, "-c", "import ghstars"], capture_output=True, text=True
    )
    if probe.returncode == 0:
        return True
    print(
        "❌ 未安装 ghstars 引擎。安装：pip install ghstars\n"
        "   （或 uv tool install ghstars；详见 https://github.com/zhe97396-tech/ghstars）",
        file=sys.stderr,
    )
    return False


def main() -> int:
    if not _ensure_package():
        return 2

    args = _translate_args(sys.argv[1:])
    if "--json" not in args:
        args.append("--json")
    env = _ghstars_env()

    proc = subprocess.run(
        [sys.executable, "-m", "ghstars", "sync", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        print(f"❌ ghstars sync 失败（exit {proc.returncode}）", file=sys.stderr)
        return proc.returncode

    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(proc.stdout)
        return 0

    if report.get("added"):
        print("\n🔢 更新向量索引…", file=sys.stderr)
        embed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "embed.py"), "--incremental"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONPATH": ""},
        )
        if embed.returncode == 0:
            tail = (embed.stdout or "").strip().splitlines()[-1:] or ["OK"]
            print(f"  ✓ {tail[0]}", file=sys.stderr)
        else:
            print(f"  ⚠ 向量索引更新失败：{(embed.stderr or '')[:300]}", file=sys.stderr)
            print("  提示：可运行 `#index` 补索引", file=sys.stderr)

    if report.get("dry_run"):
        print(
            f"⭐ DRY-RUN：待处理 {report['new']}，已有 {report['skipped']}，"
            f"待归档 {report['removed']}（未写盘、未调 LLM）"
        )
        if report.get("pending"):
            print("待处理：" + ", ".join(report["pending"][:20]))
        return 0

    print(
        f"⭐ GitHub Stars 同步完成！新增 {report['added']}，跳过 {report['skipped']}，"
        f"归档 {report['archived']}"
        + (f"，归一 {report['normalized']}" if report.get("normalized") else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
