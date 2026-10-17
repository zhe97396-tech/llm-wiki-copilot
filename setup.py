#!/usr/bin/env python3
"""llm-wiki-copilot 初始化向导

用法：
    python setup.py                  # 交互式（推荐）
    python setup.py --yes            # 全部使用默认值（CI / 快速体验）
    python setup.py --data-root D:/my-knowledge

做三件事：
    1. 生成 config.yaml（数据目录 / 分类池 / LLM / 集成开关）
    2. 在数据目录建好知识库骨架 + purpose.md
    3. 生成 Agent 适配文件（CLAUDE.md / AGENTS.md）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

DEFAULT_CATEGORIES = ["通用", "方法论", "工具与技术", "个人成长"]
RAW_PLATFORMS = ["douyin", "twitter", "web", "wechat", "bilibili", "assets"]

PURPOSE_TMPL = """# 知识库目的声明

> 本文件在每次编译时注入上下文，帮 AI 判断什么内容与你的知识库相关。
> 可以直接编辑，越具体越有用。

## 我为什么建这个知识库

{focus}

## 边界

- **聚焦领域**：{domains}
- **暂不覆盖**：{exclude}

## 期望的交互方式

- 内容进来先编译（生成总结页 + 概念页），不强制立即精读
- 想深读时用 `#start` 逐段带读
- 复盘时自动关联已有知识，不让我重复劳动
- 所有回答必须锚定原文；知识库没有的，明确说"未收录"
"""

INDEX_TMPL = """# 页面索引

> 由编译流程自动维护。新增总结页/概念页时会追加到这里。

## 总结页

（编译后自动填充）

## 概念页

（编译后自动填充）
"""

OVERVIEW_TMPL = """# 全库综合概述

> 由编译流程自动维护：每次入库后更新这里的主题地图与统计。

## 当前状态

- 总结页：0
- 概念页：0
- 最后更新：（尚未入库）

## 主题地图

（入库后自动生成）
"""

PROCESSED_TMPL = "# Processed Files Log (SHA256)\n# Format: filename:sha256\n\n"

IDEAS_TMPL = """# 待深挖灵感池

> 带读/复盘过程中冒出来、但与当篇文章主题无关的有价值话题，记在这里，日后想深挖再看。
> 由 `#distill` 自动追加；也可以手动记。

（暂无）
"""


def ask(question: str, default: str = "", allow_empty: bool = True) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        try:
            ans = input(f"{question}{hint}: ").strip()
        except EOFError:
            ans = ""
        if not ans:
            if default:
                return default
            if allow_empty:
                return ""
        else:
            return ans
        print("  请输入内容，或直接回车使用默认值。")


def build_config(data_root: Path, categories: list[str], llm: dict) -> str:
    cat_yaml = "\n".join(f"  - {c}" for c in categories)
    return f"""# llm-wiki-copilot 配置（由 setup.py 生成，可随时手改）

# ── 数据目录：知识库本体所在位置（用 Obsidian 或任意编辑器打开这里）──
data_root: "{data_root.as_posix()}"

purpose_file: "purpose.md"

# ── 分类池：编译时把总结页路由到 wiki/summaries/<分类>/ ──
categories:
{cat_yaml}

# ── LLM（可选）：用于英文翻译、星标中文标签/分类 ──
# 留空 → 这些增强步骤自动跳过，核心链路（编译/检索/带读）不受影响
llm:
  base_url: "{llm.get('base_url', '')}"
  api_key: "{llm.get('api_key', '')}"
  model: "{llm.get('model', '')}"

# ── 路径覆盖（留空自动探测）──
paths:
  tesseract: ""
  fastembed_cache: ""
  python: ""

# ── 可选集成（默认全关，用法见 integrations/README.md）──
integrations:
  github_stars:
    enabled: false
    username: ""
    token_env: GITHUB_TOKEN
  douyin:
    enabled: false
    python: ""
    transcribe_script: ""
    download_script: ""
  chubby:
    enabled: false
    path: ""
    python: ""
    cookie_xhs: ""
  opencli:
    enabled: false
    command: "opencli"
"""


def scaffold(data_root: Path, focus: str, domains: str, exclude: str, yes: bool):
    """建知识库骨架（幂等：已存在则跳过，不覆盖任何已有内容）"""
    created, skipped = [], []
    for sub in ["wiki/summaries", "wiki/concepts", "wiki/conversations", "wiki/evergreen",
                "00-INBOX", ".llm-wiki"] + [f"raw/{p}" for p in RAW_PLATFORMS]:
        p = data_root / sub
        if p.exists():
            skipped.append(sub)
        else:
            p.mkdir(parents=True, exist_ok=True)
            created.append(sub)

    seeds = {
        "purpose.md": PURPOSE_TMPL.format(focus=focus or "（写下你的目标，例如：把读过的好内容沉淀成可复用的知识体系）",
                                          domains=domains or "（例如：产品、内容创作、个人成长）",
                                          exclude=exclude or "（例如：时事新闻、纯技术文档）"),
        "wiki/index.md": INDEX_TMPL,
        "wiki/overview.md": OVERVIEW_TMPL,
        "wiki/ideas.md": IDEAS_TMPL,
        "✅ processed.md": PROCESSED_TMPL,
    }
    for rel, content in seeds.items():
        p = data_root / rel
        if p.exists():
            skipped.append(rel)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            created.append(rel)
    return created, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="llm-wiki-copilot 初始化向导")
    ap.add_argument("--yes", action="store_true", help="全部使用默认值，不交互")
    ap.add_argument("--data-root", default=None, help="知识库数据目录（默认 ./vault）")
    args = ap.parse_args()
    yes = args.yes

    print("=" * 64)
    print("  llm-wiki-copilot 初始化向导")
    print("=" * 64)
    print("  这个向导会问 3 组问题，然后生成配置与知识库骨架。")
    print("  所有内容之后都可以手改，不会覆盖任何已有文件。\n")

    # ── Q1 数据目录 ──
    default_root = args.data_root or str(REPO_ROOT / "vault")
    print("【1/3】知识库放在哪里？")
    print("       （用 Obsidian 或任意编辑器打开；与引擎分开，隐私更干净）")
    if yes:
        data_root = Path(default_root)
        print(f"       -> {data_root}")
    else:
        data_root = Path(ask("       目录路径", default_root)).expanduser().resolve()
    print()

    # ── Q2 知识库定位 ──
    print("【2/3】你的知识库聚焦什么？（会写进 purpose.md，编译时注入给 AI）")
    if yes:
        focus = domains = exclude = ""
        print("       -> 使用模板默认值")
    else:
        focus = ask("       一句话目标（可留空）", "")
        domains = ask("       聚焦领域（逗号分隔，可留空）", "")
        exclude = ask("       不覆盖什么（可留空）", "")
    print()

    # ── Q3 LLM ──
    print("【3/3】LLM 配置（可选）：用于英文翻译、星标中文标签与分类")
    print("       不配也能用 —— 核心的编译/检索/带读完全不依赖它。")
    llm = {}
    if yes:
        print("       -> 跳过（未配置）")
    else:
        if ask("       现在配置吗？(y/N)", "n").lower().startswith("y"):
            llm["base_url"] = ask("       base_url", "https://api.deepseek.com/v1")
            llm["api_key"] = ask("       api_key", "")
            llm["model"] = ask("       model", "deepseek-chat")
    print()

    # ── 写配置 ──
    cfg_path = REPO_ROOT / "config.yaml"
    if cfg_path.exists() and not yes:
        if not ask(f"  config.yaml 已存在，覆盖吗？(y/N)", "n").lower().startswith("y"):
            print("  保留原 config.yaml，仅建目录骨架。")
        else:
            cfg_path.write_text(build_config(data_root, DEFAULT_CATEGORIES, llm), encoding="utf-8")
            print(f"  ✓ 已写入 {cfg_path}")
    else:
        cfg_path.write_text(build_config(data_root, DEFAULT_CATEGORIES, llm), encoding="utf-8")
        print(f"  ✓ 已写入 {cfg_path}")

    # ── 建骨架 ──
    created, skipped = scaffold(data_root, focus, domains, exclude, yes)
    print(f"\n  ✓ 数据目录：{data_root}")
    if created:
        print(f"    新建：{len(created)} 项（{', '.join(created[:6])}{' ...' if len(created) > 6 else ''}）")
    if skipped:
        print(f"    已存在跳过：{len(skipped)} 项（未覆盖）")

    # ── 生成适配文件 ──
    ba = REPO_ROOT / "scripts" / "build_adapters.py"
    if ba.exists():
        try:
            r = subprocess.run([sys.executable, str(ba)], cwd=str(REPO_ROOT),
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
            print(f"  ✓ 已生成 Agent 适配文件（CLAUDE.md / AGENTS.md）"
                  if r.returncode == 0 else f"  ⚠ 适配文件生成失败：{r.stderr[:160]}")
        except Exception as e:
            print(f"  ⚠ 适配文件生成跳过：{e}")

    print("\n" + "=" * 64)
    print("  ✅ 初始化完成")
    print("=" * 64)
    print(f"""
  你的知识库：{data_root}
  （引擎在 {REPO_ROOT}；这个目录用 Obsidian 或任意编辑器打开都行）

  下一步：
    1. 把任意文章 / PDF / 截图丢进 {data_root / '00-INBOX'}/
    2. 对你的 AI Agent 说： #ingest
    3. 说： #start <标题>      开始深度带读
    4. 想搜东西： #search <关键词>

  可选：采集外部内容（抖音 / 平台抓取 / 搜索）见 integrations/README.md
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
