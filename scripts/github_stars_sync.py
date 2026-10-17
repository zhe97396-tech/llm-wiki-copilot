#!/usr/bin/env python3
"""GitHub Stars 同步入库脚本

将 GitHub starred 项目入库 Obsidian Wiki 的 github-stars/ 目录。
流程：API拉取 → 去重对比 → 自动分类 → 读README → LLM打中文标签 → 生成Markdown → 入库

用法：
    python github_stars_sync.py [--output-dir DIR] [--no-llm-tags] [--full]

选项：
    --output-dir DIR   入库目录（默认：脚本所在Wiki项目的 github-stars/ 目录）
    --no-llm-tags      跳过LLM打中文标签（省token，但检索精准度降低）
    --full             忽略断点续传，全量重新处理
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ============================================================
# 配置
# ============================================================

# 从 config.yaml 读取（安装向导会写入），环境变量 GITHUB_USERNAME 优先
try:
    from config import get_env_or_cfg as _cfg
    GITHUB_USERNAME = _cfg("GITHUB_USERNAME", "integrations.github_stars.username", "") or ""
except ImportError:  # 缺少 config.py 时退回环境变量
    GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "").strip()
GITHUB_API_BASE = "https://api.github.com"

# LLM API（OpenAI兼容接口，配置自动识别，与 convert.py 统一）
#   LLM_API_BASE  — API base URL (default: https://api.openai.com/v1)
#   LLM_API_KEY   — API key
#   LLM_MODEL     — Model name (default: gpt-4o-mini)
#
# 自动回退：环境变量未设置时，从 Hermes config.yaml / auth.json + .env 读取
# 当前 provider 配置（统一逻辑见 llm_api.py，换 provider 后脚本自动跟着走）
from llm_api import load_llm_config as _load_hermes_config

_hermes_base, _hermes_key, _hermes_model = _load_hermes_config()

_LLM_API_BASE = os.environ.get("LLM_API_BASE", "") or _hermes_base or "https://api.openai.com/v1"
_LLM_API_KEY = os.environ.get("LLM_API_KEY", "") or _hermes_key
_LLM_MODEL = os.environ.get("LLM_MODEL", "") or _hermes_model or "gpt-4o-mini"


# ============================================================
# LLM 调用（OpenAI兼容接口，统一走 llm_api.call_llm）
# ============================================================

def _llm_call(prompt: str, max_tokens: int = 100, temperature: float = 0.1) -> str | None:
    """调用 OpenAI 兼容 LLM 接口，返回文本内容或 None。"""
    if not _LLM_API_KEY:
        return None
    from llm_api import call_llm
    result = call_llm(prompt, max_tokens=max_tokens, temperature=temperature, timeout=60)
    return result or None

# README文件名候选
README_CANDIDATES = ["README.md", "readme.md", "README.rst", "readme.rst", "README.txt", "readme.txt", "README"]

# 分类映射表
CATEGORY_MAP = {
    "ai-agents": ["ai-agents", "ai-agent", "agent", "agentic-ai"],
    "claude-code-skills": ["claude-skills", "agent-skills", "claude-skill", "claude-code-skill", "skills", "skill", "codex"],
    "mcp-tools": ["mcp"],
    "rag": ["rag"],
    "llm-tools": ["llm", "prompt-engineering", "openai"],
    "browser-automation": ["browser-automation", "web", "scraping"],
    "devops": ["devops", "ci-cd", "docker", "kubernetes"],
    "tools-utilities": ["cli", "developer-tools", "tools", "utility"],
    "xiaohongshu": ["xiaohongshu"],
    "resume-career": ["resume", "career", "job"],
    "image-gen": ["image-generation", "ai-art", "stable-diffusion"],
}

# ============================================================
# GitHub API
# ============================================================

def get_github_token():
    """获取GitHub Token：优先环境变量，其次gh CLI"""
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        import subprocess
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def github_api_get(url, token=""):
    """GitHub API GET请求"""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_stars(token=""):
    """拉取所有starred项目元信息"""
    stars = []
    page = 1
    while True:
        url = f"{GITHUB_API_BASE}/users/{GITHUB_USERNAME}/starred?per_page=100&page={page}"
        data = github_api_get(url, token)
        if not data:
            break
        for r in data:
            stars.append({
                "full_name": r["full_name"],
                "description": r.get("description") or "",
                "stargazers_count": r["stargazers_count"],
                "language": r.get("language") or "",
                "topics": r.get("topics") or [],
                "html_url": r["html_url"],
                "starred_at": r.get("starred_at", ""),
            })
        print(f"  ✓ 第{page}页：{len(data)}个项目", file=sys.stderr)
        page += 1
    return stars


def fetch_readme(full_name, token=""):
    """获取README前200行"""
    for name in README_CANDIDATES:
        url = f"{GITHUB_API_BASE}/repos/{full_name}/contents/{name}"
        try:
            headers = {"Accept": "application/vnd.github.v3.raw"}
            if token:
                headers["Authorization"] = f"token {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                lines = content.split("\n")[:200]
                return "\n".join(lines)
        except urllib.error.HTTPError:
            continue
        except Exception:
            continue
    return ""


# ============================================================
# 分类
# ============================================================

def classify_project(project, output_dir=None, readme_content=""):
    """根据topics和描述推断分类。映射表优先→描述关键词→LLM兜底(含README摘要)→other"""
    topics = [t.lower() for t in project["topics"]]
    desc_lower = project.get("description", "").lower()

    # 第一优先：topics匹配映射表
    for category, keywords in CATEGORY_MAP.items():
        for kw in keywords:
            if kw.lower() in topics:
                return category

    # 第二优先：描述关键词推断
    desc_keywords = {
        "ai-agents": ["agent", "agentic", "autonomous"],
        "claude-code-skills": ["claude", "codex", "skill", "cursor"],
        "mcp-tools": ["mcp", "model context protocol"],
        "llm-tools": ["llm", "prompt", "gpt", "language model"],
        "browser-automation": ["browser", "scraping", "crawl", "selenium", "playwright"],
        "devops": ["docker", "kubernetes", "ci/cd", "deploy", "infra"],
        "tools-utilities": ["cli", "tool", "utility", "framework"],
        "image-gen": ["image", "diffusion", "generation", "art"],
    }
    for category, keywords in desc_keywords.items():
        for kw in keywords:
            if kw in desc_lower:
                return category

    # 第三优先：LLM从已有分类中选（不新建目录，保证分类名统一）
    if output_dir and _LLM_API_KEY:
        existing_categories = sorted([
            d.name for d in output_dir.iterdir()
            if d.is_dir() and any(d.glob("*.md"))
        ])
        if existing_categories:
            try:
                categories_str = "、".join(existing_categories)
                readme_summary = readme_content[:2000] if readme_content else "（无README）"
                prompt = f"""根据以下GitHub项目信息，从已有分类中选择最匹配的一个分类。
已有分类：{categories_str}
⚠️ 注意：topics里的兼容性标签（claude-code/anthropic/claude等）不代表项目形态，要看项目本质（是什么工具/什么形态）。
只输出分类名，不要解释。如果都不匹配，输出 other。

项目名：{project['full_name']}
描述：{project['description']}
Topics：{', '.join(project['topics']) if project['topics'] else '无'}
README摘要：
{readme_summary}"""

                llm_result = _llm_call(prompt, max_tokens=50)
                if llm_result:
                    llm_category = llm_result.strip().lower()
                    if llm_category in existing_categories:
                        return llm_category
            except Exception:
                pass  # LLM失败不影响流程

    return "other"


# ============================================================
# LLM打中文标签
# ============================================================

def generate_chinese_tags(project, readme_content):
    """用LLM生成中文场景标签"""
    if not _LLM_API_KEY:
        return [], ""

    topics_str = "、".join(project["topics"]) if project["topics"] else "无"
    readme_summary = readme_content[:2000] if readme_content else "（无README）"

    prompt = f"""根据以下GitHub项目信息，生成：
1. 2-3句中文描述（第1句概括核心功能，第2-3句说明适用场景和用户群体，总共40-80字）
2. 3-5个中文场景标签（用于中文检索）

严格按以下格式输出，不要加其他内容：
描述：xxx。适用于xxx等场景。
标签：xxx, xxx, xxx

项目名：{project['full_name']}
描述：{project['description']}
Topics：{topics_str}
README摘要：
{readme_summary}"""

    try:
        content = _llm_call(prompt, max_tokens=300)
        if not content:
            return [], ""

        content = content.strip()
        # 解析中文描述
        chinese_desc = ""
        desc_match = re.search(r"描述[：:]\s*(.+)", content)
        if desc_match:
            chinese_desc = desc_match.group(1).strip()
        # 解析标签
        tags = []
        tags_match = re.search(r"标签[：:]\s*(.+)", content)
        if tags_match:
            tags = re.split(r"[,，、\s]+", tags_match.group(1))
            tags = [t.strip() for t in tags if t.strip()][:5]
        return tags, chinese_desc
    except Exception as e:
        print(f"  ⚠ LLM标签失败({e})，跳过", file=sys.stderr)
        return [], ""


# ============================================================
# Markdown生成
# ============================================================

def generate_markdown(project, category, chinese_tags, chinese_desc=""):
    """生成单个项目的Markdown文件"""
    now = datetime.now().strftime("%Y-%m-%d")
    topics_yaml = ", ".join(project["topics"]) if project["topics"] else ""
    tags_yaml = ", ".join(chinese_tags) if chinese_tags else ""
    desc = project["description"] or project["full_name"]

    # 文件名：用repo名（owner/name → owner-name）
    filename = project["full_name"].replace("/", "-") + ".md"

    # 中文描述块（如果有）
    desc_block = f"\n{chinese_desc}" if chinese_desc else ""

    content = f"""---
title: {project['full_name'].split('/')[-1]}
type: github-star
platform: github
source: {project['html_url']}
repo: {project['full_name']}
author: {project['full_name'].split('/')[0]}
stars: {project['stargazers_count']}
language: {project['language']}
topics: [{topics_yaml}]
starred_at: {project['starred_at'][:10] if project['starred_at'] else now}
category: {category}
tags: [{tags_yaml}]
---

# {project['full_name'].split('/')[-1]}

{desc}{desc_block}

🔗 [GitHub]({project['html_url']})
"""
    return filename, content


def generate_index(output_dir, categories_count):
    """生成总览索引"""
    lines = ["# GitHub Stars 索引\n"]
    lines.append(f"> 更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    lines.append("## 分类概览\n")

    for cat_dir in sorted(output_dir.iterdir()):
        if cat_dir.is_dir():
            count = len(list(cat_dir.glob("*.md")))
            if count > 0:
                lines.append(f"- **{cat_dir.name}/** ({count}个项目)")

    return "\n".join(lines)


# ============================================================
# 去重与增量
# ============================================================

def scan_existing_repos(output_dir):
    """扫描库里已有的repo列表"""
    existing = {}
    for md_file in output_dir.rglob("*.md"):
        if md_file.name == "_index.md":
            continue
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                content = f.read()
            # 从frontmatter提取repo字段；统一小写做键（GitHub repo 名大小写不敏感，
            # 曾因 Hmbown/CodeWhale 与 Hmbown/Codewhale 大小写差异导致同一项目重复入库）
            match = re.search(r"^repo:\s*(.+)$", content, re.MULTILINE)
            if match:
                existing[match.group(1).strip().lower()] = md_file
        except Exception:
            continue
    return existing


def load_sync_state(output_dir):
    """加载断点续传状态"""
    state_file = output_dir / ".sync_state.json"
    if state_file.exists():
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"processed": []}


def save_sync_state(output_dir, state):
    """保存断点续传状态（写入前去重，防止重复记录）"""
    state_file = output_dir / ".sync_state.json"
    # 去重：保持顺序，去掉重复的repo
    if "processed" in state:
        seen = set()
        deduped = []
        for repo in state["processed"]:
            if repo not in seen:
                seen.add(repo)
                deduped.append(repo)
        state["processed"] = deduped
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# 主流程
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="GitHub Stars 同步入库")
    parser.add_argument("--output-dir", default="", help="入库目录")
    parser.add_argument("--no-llm-tags", action="store_true", help="跳过LLM打中文标签")
    parser.add_argument("--full", action="store_true", help="全量重新处理（忽略断点续传）")
    args = parser.parse_args()

    print("=" * 50, file=sys.stderr)
    print("GitHub Stars 同步入库", file=sys.stderr)
    print("=" * 50, file=sys.stderr)

    # 确定输出目录
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        # 默认：知识库数据目录下的 github-stars/
        # 必须与索引器一致（_utils.GITHUB_STARS_DIR），否则 #stars 入库的内容搜不到
        from _utils import GITHUB_STARS_DIR
        output_dir = GITHUB_STARS_DIR

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"📁 入库目录：{output_dir}", file=sys.stderr)

    # Step 1：获取GitHub Token
    token = get_github_token()
    if not token:
        print("❌ 未找到GitHub Token！请运行 gh auth login 或设置 GITHUB_TOKEN 环境变量", file=sys.stderr)
        sys.exit(1)
    print("✓ GitHub Token已获取", file=sys.stderr)

    # Step 2：拉取star列表
    print("\n📍 Step 1：拉取GitHub star列表...", file=sys.stderr)
    stars = fetch_all_stars(token)
    print(f"  ✓ 共{len(stars)}个项目", file=sys.stderr)

    # Step 3：去重对比
    print("\n🔍 Step 2：去重对比...", file=sys.stderr)
    existing = scan_existing_repos(output_dir)
    api_repos = {s["full_name"].lower() for s in stars}   # 小写归一，与 existing 键口径一致
    existing_repos = set(existing.keys())

    new_repos = api_repos - existing_repos
    removed_repos = existing_repos - api_repos
    skip_count = len(api_repos & existing_repos)

    print(f"  ✓ 新增：{len(new_repos)}，跳过：{skip_count}，取消star：{len(removed_repos)}", file=sys.stderr)

    # Step 4：处理新增项目
    new_projects = [s for s in stars if s["full_name"].lower() in new_repos]
    added_count = 0
    llm_tags_enabled = not args.no_llm_tags

    if new_projects:
        print(f"\n📥 Step 3：处理{len(new_projects)}个新增项目...", file=sys.stderr)

        # 断点续传
        state = load_sync_state(output_dir)
        if not args.full:
            processed = set(state.get("processed", []))
            new_projects = [p for p in new_projects if p["full_name"] not in processed]

        for i, project in enumerate(new_projects):
            full_name = project["full_name"]
            print(f"\n  [{i+1}/{len(new_projects)}] {full_name}", file=sys.stderr)

            # 读README（先读，LLM兜底分类需要摘要）
            readme = ""
            try:
                readme = fetch_readme(full_name, token)
                if readme:
                    print(f"    ✓ README：{len(readme.split(chr(10)))}行", file=sys.stderr)
                else:
                    print(f"    ⚠ 无README，降级处理", file=sys.stderr)
            except Exception as e:
                print(f"    ⚠ README获取失败({e})，降级处理", file=sys.stderr)

            # 分类（LLM兜底参考README摘要）
            category = classify_project(project, output_dir, readme)
            cat_dir = output_dir / category
            cat_dir.mkdir(parents=True, exist_ok=True)

            # LLM打中文标签+描述
            chinese_tags = []
            chinese_desc = ""
            if llm_tags_enabled:
                chinese_tags, chinese_desc = generate_chinese_tags(project, readme)
                if chinese_tags:
                    print(f"    🏷 标签：{', '.join(chinese_tags)}", file=sys.stderr)
                else:
                    print(f"    ⚠ 标签生成失败，跳过", file=sys.stderr)
                if chinese_desc:
                    print(f"    📝 中文描述：{chinese_desc}", file=sys.stderr)

            # 生成Markdown
            filename, content = generate_markdown(project, category, chinese_tags, chinese_desc)
            filepath = cat_dir / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"    ✓ 入库：{category}/{filename}", file=sys.stderr)

            added_count += 1

            # 更新断点续传状态（set去重，防止重复追加）
            processed_list = state.setdefault("processed", [])
            if full_name not in processed_list:
                processed_list.append(full_name)
            save_sync_state(output_dir, state)

            # 限流：每50个项目间隔1秒
            if (i + 1) % 50 == 0:
                time.sleep(1)

    # Step 5：处理取消star（幂等：已归档不再叠加后缀；历史叠加态一并归一）
    archived_count = 0
    normalized_count = 0
    if removed_repos:
        print(f"\n🗑 Step 4：归档{len(removed_repos)}个取消star的项目...", file=sys.stderr)
        for repo in removed_repos:
            filepath = existing[repo]
            # 在frontmatter中标记archived
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                # 精确匹配整个 type 值，避免 replace() 子串误匹配导致 -archived 无限叠加
                # （search.py 的 SOURCE_TIER / --exclude-type 都是精确匹配，type 必须是规范值）
                m = re.search(r"^type:[ \t]*(github-star(?:-archived)*)[ \t]*$", content, re.MULTILINE)
                if not m:
                    print(f"  ⚠ 跳过（type 非 github-star 系列）：{repo}", file=sys.stderr)
                    continue
                old_type = m.group(1)
                if old_type == "github-star-archived":
                    # 已是规范归档态：幂等跳过，不写盘（否则每次同步都会再叠一层）
                    continue
                content = content[:m.start(1)] + "github-star-archived" + content[m.end(1):]
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                if old_type == "github-star":
                    archived_count += 1
                    print(f"  ✓ 归档：{repo}", file=sys.stderr)
                else:
                    normalized_count += 1
                    print(f"  🔧 归一（历史后缀叠加）：{repo}  {old_type} → github-star-archived", file=sys.stderr)
            except Exception as e:
                print(f"  ⚠ 归档失败({e})：{repo}", file=sys.stderr)

    # Step 6：自动建立向量索引（新增项目立即可搜）
    if added_count > 0:
        print("\n🔢 Step 6：自动建立向量索引...", file=sys.stderr)
        try:
            import subprocess
            # 用当前解释器跑 embed.py（安装向导保证它装了 zvec/fastembed）
            embed_python = sys.executable
            embed_script = str(Path(__file__).parent / "embed.py")
            env = {**os.environ, "PYTHONPATH": ""}
            result = subprocess.run([embed_python, embed_script, "--incremental"],
                                    capture_output=True, text=True, timeout=120, env=env)
            if result.returncode == 0:
                print("  ✓ 向量索引已更新", file=sys.stderr)
            else:
                print(f"  ⚠ 向量索引更新失败: {result.stderr[:200]}", file=sys.stderr)
                print("  提示：可手动运行 #index 更新索引", file=sys.stderr)
        except Exception as e:
            print(f"  ⚠ 向量索引更新失败({e})", file=sys.stderr)
            print("  提示：可手动运行 #index 更新索引", file=sys.stderr)

    # Step 7：生成总览索引
    print("\n📝 Step 5：生成总览索引...", file=sys.stderr)
    index_content = generate_index(output_dir, {})
    with open(output_dir / "_index.md", "w", encoding="utf-8") as f:
        f.write(index_content)
    print("  ✓ _index.md 已生成", file=sys.stderr)

    # 汇报
    print("\n" + "=" * 50, file=sys.stderr)
    print("✅ 同步完成！", file=sys.stderr)
    print("=" * 50, file=sys.stderr)
    print(f"  新增：{added_count}", file=sys.stderr)
    print(f"  跳过：{skip_count}", file=sys.stderr)
    print(f"  归档：{archived_count}", file=sys.stderr)
    if normalized_count:
        print(f"  归一（历史 -archived 叠加修复）：{normalized_count}", file=sys.stderr)
    print(f"  目录：{output_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
