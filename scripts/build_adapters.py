#!/usr/bin/env python3
"""
Adapter builder: generate platform-specific entry files from system/SKILL.md.

Reads the platform-neutral routing template and generates:
  - CLAUDE.md  (Hermes / Claude Code)
  - AGENTS.md  (Codex / OpenCode)
  - GEMINI.md  (Gemini CLI)

Each output file has:
  - Platform-specific intro ("你正在通过 Hermes 运行" vs "Codex" vs "Gemini")
  - Identical instruction table (from system/SKILL.md)
  - Identical red lines and file structure

Usage:
    python scripts/build_adapters.py              # generate all
    python scripts/build_adapters.py --platform hermes   # generate one
    python scripts/build_adapters.py --dry-run     # preview without writing
"""

import argparse
import sys
from pathlib import Path

# Bootstrap shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from _utils import REPO_ROOT, read_file, write_file
from config import data_root as _data_root

# ── Platform definitions ───────────────────────────────────────────────

PLATFORMS = {
    # Claude Code 约定（多数 Agent 兼容：Claude Code / Cursor / Windsurf 等）
    "claude": {
        "filename": "CLAUDE.md",
        "agent_name": "Claude Code 及兼容约定",
        "intro": "你正在通过一个 Claude Code 约定的 Agent（Claude Code / Cursor / Windsurf / Hermes 等）运行本知识副驾。",
    },
    "codex": {
        "filename": "AGENTS.md",
        "agent_name": "Codex CLI / OpenCode",
        "intro": "你正在通过 **Codex CLI / OpenCode** 运行本知识副驾。",
    },
    "gemini": {
        "filename": "GEMINI.md",
        "agent_name": "Gemini CLI",
        "intro": "你正在通过 **Gemini CLI** 运行本知识副驾。",
    },
}

# ── 平台原生命令（斜杠命令）──────────────────────────────────────────
# 各平台机制核实结果（2026-10）：
#   Claude Code  .claude/commands/<name>.md    → /<name>          ✅ 活跃
#   Gemini CLI   .gemini/commands/<name>.toml  → /<name>          ✅ 活跃（TOML 格式）
#   Codex CLI    ~/.codex/prompts/<name>.md    → /prompts:<name>  ❌ 0.117.0 起已移除，官方要求改用 skills
#
# 命令文件必须写在 data_root（用户工作目录）：Claude Code 只认工作目录下的
# .claude/commands/，写在引擎目录不会被加载。

SLASH_TARGETS = {
    "claude": {"rel": Path(".claude") / "commands", "ext": ".md"},
    "gemini": {"rel": Path(".gemini") / "commands", "ext": ".toml"},
}

CLAUDE_COMMAND = """---
description: {desc}
---

执行知识副驾的 `#{name}` 流程。

1. 读取指令定义文件：`{engine}/system/commands/{name}.md`（数据目录由 config.yaml 的 data_root 指定）
2. 严格按该文件执行，不要凭记忆简化步骤
3. 第一行打印：`[⚡系统拦截：正在严格执行 #{name} 流程...]`

用户附加说明：$ARGUMENTS
"""

GEMINI_COMMAND = """description = "{desc}"
prompt = \"\"\"
执行知识副驾的 `#{name}` 流程。

1. 读取指令定义文件：`{engine}/system/commands/{name}.md`（数据目录由 config.yaml 的 data_root 指定）
2. 严格按该文件执行，不要凭记忆简化步骤
3. 第一行打印：`[⚡系统拦截：正在严格执行 #{name} 流程...]`

{{{{args}}}}
\"\"\"
"""


def read_command_meta() -> list:
    """从 system/commands/*.md 的 frontmatter 读出 (name, description)。"""
    cmds_dir = REPO_ROOT / "system" / "commands"
    out = []
    for f in sorted(cmds_dir.glob("*.md")):
        text = read_file(f)
        name = desc = ""
        for line in text.splitlines()[:12]:
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip('"\'')
            elif line.startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip('"\'')
        out.append((name or f.stem, desc or f"#{f.stem}"))
    return out


def build_slash_commands(dry_run: bool = False) -> None:
    """生成各平台原生命令文件到 data_root/.<platform>/commands/。"""
    root = _data_root()
    engine = REPO_ROOT.as_posix()
    cmds = read_command_meta()
    print(f"\n  📁 斜杠命令 → {root}")
    for platform, cfg in SLASH_TARGETS.items():
        tpl = CLAUDE_COMMAND if platform == "claude" else GEMINI_COMMAND
        target_dir = root / cfg["rel"]
        if dry_run:
            print(f"     [{platform}] {len(cmds)} 个 → {target_dir}")
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, desc in cmds:
            content = tpl.format(name=name, desc=desc, engine=engine)
            write_file(target_dir / f"{name}{cfg['ext']}", content)
        print(f"     ✅ [{platform}] {len(cmds)} 个 → {target_dir}")
    print("     ℹ Codex CLI 已移除自定义命令（0.117.0），请用 skills 或 `#` 触发")


# ── Template (2026-08-01 同步: 15指令 + Step1-9编译流程 + RAG脚本清单) ──

TEMPLATE = """# LLM Wiki 知识副驾

> **{agent_name} 路由层** — 详细指令定义在 `system/SKILL.md`，各指令详细步骤在 `system/commands/` 目录。

{intro}

> **⚠️ 路径约定（先读这条）**：本文档与 `system/` 中出现的 `wiki/`、`raw/`、`00-INBOX/`、`wiki_log.md`
> 等所有数据路径，都是相对 **`config.yaml` 里的 `data_root`（你的知识库目录）**，
> 而不是引擎所在目录。引擎只放代码与提示词，知识内容在 data_root。

## 核心角色

你是一位严谨的"知识副驾"和"全能私教"。核心任务是陪伴用户深度消化已编译好的本地笔记。

## 指令触发协议

当用户输入包含以下指令时，代表**系统级命令调用**，绝对不是普通聊天：

| 指令 | 中文 | 说明 |
|------|------|------|
| `#start` | `#开始` | 深度学习四步法（先过滤已沉淀文章，不重复带读） |
| `#distill` | `#复盘` `#总结` | 深度解读版（思维导图+流程图+认知逻辑重组解读+对话增量+观点标注） |
| `#bridge` | `#织网` | 消灭孤岛 |
| `#search` | `#搜索` | 全库语义搜索 |
| `#index` | `#索引` | Zvec索引管理 |
| `#ask` | `#问` | 知识库问答：脚本判断复杂度，简单直接答，复杂先引导再整合，缺口主动建议搜索 |
| `#research` | `#调研` | 主动搜索补充知识 |
| `#lint` | `#纠错` | 逻辑冲突审查 |
| `#evergreen` | `#常青` | 知识升华 |
| `#ingest` | `#入库` | 批量编译入库：INBOX格式转换→编译→全局更新 |
| `#archive` | `#冷宫` | 断舍离 |
| `#stars` | `#星标` `#收藏` | GitHub Stars 同步入库 |

**触发拦截后**：
1. 停止闲聊
2. 读取 `system/SKILL.md` 获取指令索引
3. 读取 `system/commands/[指令名].md` 获取详细执行步骤
4. 回复第一行打印：`[⚡系统拦截：正在严格执行 #xxx 流程...]`

## 斜杠命令（各平台原生入口）

本项目为 **Claude Code** 与 **Gemini CLI** 生成了原生命令，与 `#指令` 完全等价：

| 平台 | 命令位置 | 调用 |
|---|---|---|
| Claude Code | `<data_root>/.claude/commands/<name>.md` | `/<name>` |
| Gemini CLI | `<data_root>/.gemini/commands/<name>.toml` | `/<name>` |
| Codex CLI | ——（自定义命令已在 0.117.0 移除） | 用 `#指令` 或 skills |

12 个命令与 12 个 `#指令` 一一对应。命令文件只是**转发层**：正文指向
`system/commands/<name>.md`，真正的步骤定义只有一份，不会产生两份真相。

重新生成：`python scripts/build_adapters.py`

**绝对红线**：
1. 锚定原文，知识库没有的明确说"未收录"
2. 解读知识现实意义，禁止分析写作手法
3. 克制输出，精简回答

## 系统文件结构

```
system/
  SKILL.md          ← 指令索引层
  commands/          ← 各指令详细定义（含 #ingest 编译入库）
  PROMPT.md          ← 编译提示词（Step1前置→Step2分析→Step3生成→Step3.5实体索引→Step4 RAG索引→Step5去重→Step6进化→Step7全局更新→Step8织网→Step9分类路由）
  COMPILE.md         ← 双轨编译规则与模板
scripts/
  _utils.py          ← 共享工具函数
  search.py          ← Zvec 混合检索（jieba FTS+向量+RRF+双链邻居+实体搭桥），降级 CJK bigram
  ask.py             ← 智能问答（ask.py 判断复杂度+覆盖度评分+缺口检测）
  embed.py           ← 向量化+Zvec入库（全量/增量/单文件/重建）
  entity_index.py    ← 实体索引（SQLite三表+搭桥搜索）
  zvec_config.py     ← Zvec schema/配置/doc ID编码
  health.py          ← 纯确定性健康检查（0 LLM）
  convert.py         ← 多格式→Markdown转换（MarkItDown统一引擎：PDF/DOCX/PPTX/XLSX/EPUB/HTML/CSV/图片OCR）
  watch.py           ← INBOX 文件监听器
  build_adapters.py  ← 平台适配器构建脚本
  dashboard.py       ← 看板HTML生成
  github_stars_sync.py ← GitHub Stars 同步入库脚本
github-stars/         ← GitHub Stars 仓库（#stars 触发同步）
wiki/
  summaries/         ← 总结文档（编译产物，核心主干）
  concepts/          ← 概念页（编译时自动提取/更新）
  conversations/     ← 深度解读版（#复盘产出，已纳入Zvec索引）
  evergreen/         ← 常青综述（#evergreen 产出）
  overview.md        ← 全库综合概述（编译时自动更新）
  index.md           ← 页面索引（编译时自动更新）
purpose.md           ← 知识库目的声明（编译时注入上下文）
```
"""


def build_platform(platform_key: str, dry_run: bool = False) -> str:
    """Generate entry file for a specific platform."""
    config = PLATFORMS[platform_key]
    content = TEMPLATE.format(
        agent_name=config["agent_name"],
        intro=config["intro"],
    )

    if not dry_run:
        output_path = REPO_ROOT / config["filename"]
        write_file(output_path, content)
        print(f"  ✅ {config['filename']} ({len(content)} bytes)")
    else:
        print(f"  📄 {config['filename']} would be ({len(content)} bytes)")

    return content


def main():
    parser = argparse.ArgumentParser(description="Build platform-specific adapter files")
    parser.add_argument("--platform", choices=list(PLATFORMS.keys()),
                        help="Build only one platform (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing files")
    parser.add_argument("--no-slash", action="store_true",
                        help="Skip generating platform slash commands")
    args = parser.parse_args()

    print("🔧 Adapter Builder")
    print("=" * 40)

    if args.platform:
        build_platform(args.platform, dry_run=args.dry_run)
    else:
        for key in PLATFORMS:
            build_platform(key, dry_run=args.dry_run)

    if not args.no_slash:
        build_slash_commands(dry_run=args.dry_run)

    print("=" * 40)
    if args.dry_run:
        print("Dry run complete. No files written.")
    else:
        print("Build complete.")


if __name__ == "__main__":
    main()
