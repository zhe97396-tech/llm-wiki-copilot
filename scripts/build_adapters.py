#!/usr/bin/env python3
"""
Adapter builder: generate platform-specific entry files from system/SKILL.md.

Reads the platform-neutral routing template and generates:
  - CLAUDE.md  (Claude Code 及兼容约定)
  - AGENTS.md  (Codex / OpenCode / 其它遵循该约定的 agent)
And, optionally, the native slash commands of each configured agent target
(see `adapters.slash` in config.yaml; presets: claude / opencode / pi).

Each output file has:
  - Platform-specific intro
  - Identical instruction table (from system/SKILL.md)
  - Identical red lines and file structure

Usage:
    python scripts/build_adapters.py              # generate all
    python scripts/build_adapters.py --platform claude   # generate one
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
}

# ── 平台原生命令（斜杠命令）──────────────────────────────────────────
# 设计原则：**不硬编码平台清单**。下面是已核实的预设；用户可在 config.yaml 的
# adapters.slash 里增删，或给一个自定义目录来支持任意 agent。
#
# 已核实（2026-10）—— 都属「工作目录 + 约定目录 + Markdown」这一族：
#   Claude Code   .claude/commands/<name>.md     → /<name>
#   OpenCode      .opencode/commands/<name>.md   → /<name>
#   pi            .pi/prompts/<name>.md          → /<name>
#
# 未能通用化的（如实标注，不硬编码）：
#   Codex CLI     自定义命令已于 0.117.0 移除，官方要求改用 skills
#   Gemini CLI    已被 Antigravity CLI 取代（2026-06）
#   DSH           走 plugin 机制（不是「目录 + Markdown」），需单独适配
#
# 这些 agent 只认「当前工作目录」下的约定目录，所以命令要生成到 data_root。

SLASH_PRESETS = {
    "claude":   {"dir": ".claude/commands",   "ext": ".md"},
    "opencode": {"dir": ".opencode/commands", "ext": ".md"},
    "pi":       {"dir": ".pi/prompts",        "ext": ".md"},
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


def resolve_slash_targets() -> list:
    """解析要生成哪些目标的命令。

    优先级：config.yaml 的 `adapters.slash` > 内置预设 `SLASH_PRESETS`。

    配置示例（config.yaml）：
        adapters:
          slash: [claude, opencode, pi]        # 用预设
          # 或自定义任意 agent（只要你查清它的目录约定）：
          # slash:
          #   - dir: ".myagent/prompts"          # 相对 data_root
          #     name: myagent
    """
    try:
        from config import get as _cfg_get
        cfg = _cfg_get("adapters.slash", None)
    except Exception:
        cfg = None

    items = cfg if isinstance(cfg, list) and cfg else list(SLASH_PRESETS)
    out = []
    for it in items:
        if isinstance(it, str):
            if it in SLASH_PRESETS:
                spec = dict(SLASH_PRESETS[it])
                spec["name"] = it
            else:
                spec = {"name": it, "dir": f".{it}/commands", "ext": ".md"}
        elif isinstance(it, dict) and it.get("dir"):
            spec = {"name": it.get("name") or Path(it["dir"]).name,
                    "dir": it["dir"], "ext": it.get("ext", ".md")}
        else:
            continue
        out.append(spec)
    return out


def build_slash_commands(dry_run: bool = False) -> None:
    """生成原生斜杠命令到 data_root 下各目标的约定目录。"""
    root = _data_root()
    engine = REPO_ROOT.as_posix()
    cmds = read_command_meta()
    targets = resolve_slash_targets()

    print("")
    print(f"  📁 斜杠命令 → {root}")
    if not targets:
        print("     （config.yaml 的 adapters.slash 为空，跳过）")
        return
    for tgt in targets:
        target_dir = root / tgt["dir"]
        if dry_run:
            print(f"     [{tgt['name']}] {len(cmds)} 个 → {target_dir}")
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, desc in cmds:
            content = CLAUDE_COMMAND.format(name=name, desc=desc, engine=engine)
            write_file(target_dir / f"{name}{tgt['ext']}", content)
        print(f"     ✅ [{tgt['name']}] {len(cmds)} 个 → {target_dir}")
    print("     ℹ 想支持别的 agent？在 config.yaml 的 adapters.slash 里加一行即可")


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

## 斜杠命令（原生入口）

除 `#指令` 外，`scripts/build_adapters.py` 会为各 agent 生成**原生斜杠命令**，两者完全等价：

| Agent | 命令位置（都在 `data_root` 下） | 调用 |
|---|---|---|
| Claude Code | `.claude/commands/<name>.md` | `/<name>` |
| OpenCode | `.opencode/commands/<name>.md` | `/<name>` |
| pi | `.pi/prompts/<name>.md` | `/<name>` |
| 其他任意 agent | 见下（可自行扩展） | 用 `#指令`，或按需扩展 |

**不限于上面的清单** —— 生成哪些目标由 `config.yaml` 决定：

```yaml
adapters:
  slash: [claude, opencode, pi]        # 用预设
  # 或给任意 agent 加自定义目录（只要你查清它的约定目录）：
  # slash:
  #   - dir: ".myagent/prompts"
  #     name: myagent
```

> 未通用化的：**Codex CLI**（自定义命令 0.117.0 已移除，官方要求改用 skills）、
> **Gemini CLI**（已被 Antigravity CLI 取代）、**DSH**（plugin 机制，非目录约定）——
> 这三个用 `#指令` 即可，功能不受影响。

12 个命令与 12 个 `#指令` 一一对应。命令文件只是**转发层**：正文指向
`system/commands/<name>.md`，真正的步骤定义只有一份。

重新生成：`python scripts/build_adapters.py`

## 输入入口（先判断用户给的是什么）

| 用户给的 | 怎么处理 |
|---|---|
| 网页链接（文章/博客/公众号/知乎等） | 抓正文 → 转 Markdown → 落 `00-INBOX/` |
| **抖音链接（一条或多条）** | 跑转录脚本 → 落 `00-INBOX/`（见下） |
| PDF / 图片 / 文档 | 直接放进 `00-INBOX/`，`scripts/convert.py` 会自动转换 |
| 复制粘贴的文本 | 存成 `.md` 落 `00-INBOX/` |

**抖音链接必须先转录**，不要直接丢进 `#ingest`：

```bash
# 单条
<config.integrations.douyin.python> <config.integrations.douyin.transcribe_script> "<URL>"

# 多条（用户一次给了多个链接时务必用批量脚本，不要逐条调用）
#   把链接写进文本文件，每行一条
<config.integrations.douyin.python> "<douyin 脚本目录>/douyin_batch.py" --file urls.txt --output-dir "<data_root>/00-INBOX"
```

批量脚本会：**模型只加载一次**（N 条复用，这是主要提速点）→ 逐条转录 →
按 `video_id` 记账去重（已转录的跳过，**中断可续跑**）。

所有内容落进 `00-INBOX/` 后，统一走 `#ingest` 编译。

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
  build_adapters.py  ← 平台适配器构建（入口文件 + 各 agent 原生斜杠命令）
  dashboard.py       ← 看板HTML生成
  github_stars_sync.py ← GitHub Stars 同步入库脚本
  config.py          ← 统一配置加载（环境变量 > config.yaml > 默认）
  llm_api.py         ← LLM 调用统一入口（可选增强；未配置则优雅跳过）
  check_distilled.py ← 带读前查重（#start 的前置过滤）
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
    # Windows 管道/控制台默认 GBK：emoji 会让 print 崩掉（工作其实已完成）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
