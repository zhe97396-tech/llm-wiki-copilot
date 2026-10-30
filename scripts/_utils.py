#!/usr/bin/env python3
"""
Shared utilities for LLM Wiki scripts.

Centralizes: read_file, write_file, sha256, extract_wikilinks,
all_wiki_pages, append_log, and path constants.

Distilled from llm-wiki-agent (SamurAIGPT) tools/_utils.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# ── Sys.path 净化：剔除宿主运行时注入的 PYTHONPATH ─────────────────────
# 某些 Agent 宿主（如 Hermes）会在终端环境注入自己的 venv 到 PYTHONPATH，
# 其优先级高于本项目独立 venv，导致 PIL/fastembed 加载到错误版本、Zvec
# 初始化失败并静默降级为 bigram（语义检索失真）。
# 所有脚本经 _utils.py 统一净化；干净环境无匹配路径时零副作用。
_PYTHONPATH_POLLUTION_MARKERS = ("hermes-agent", "site-packages")
for _p in list(sys.path):
    if all(m in _p for m in _PYTHONPATH_POLLUTION_MARKERS):
        try:
            sys.path.remove(_p)
        except ValueError:
            pass
del _PYTHONPATH_POLLUTION_MARKERS

# ── Path Constants ─────────────────────────────────────────────────────
# 引擎与数据分离：引擎目录只放代码与提示词，知识库内容全在 DATA_ROOT。

REPO_ROOT = Path(__file__).parent.parent     # 引擎根（scripts/ 的父目录）
SYSTEM_DIR = REPO_ROOT / "system"

# 数据根：config.yaml 的 data_root > 环境变量 LLM_WIKI_DATA > 引擎同级 ./vault
try:
    from config import data_root as _resolve_data_root  # type: ignore
    DATA_ROOT = _resolve_data_root()
except Exception:
    _env_root = os.environ.get("LLM_WIKI_DATA", "").strip()
    DATA_ROOT = Path(_env_root).expanduser().resolve() if _env_root else (REPO_ROOT / "vault")

WIKI_DIR = DATA_ROOT / "wiki"
RAW_DIR = DATA_ROOT / "raw"
INBOX_DIR = DATA_ROOT / "00-INBOX"
INDEX_DIR = DATA_ROOT / "index"
GITHUB_STARS_DIR = DATA_ROOT / "github-stars"

INDEX_FILE = WIKI_DIR / "index.md"
LOG_FILE = DATA_ROOT / "wiki_log.md"
OVERVIEW_FILE = WIKI_DIR / "overview.md"
PROCESSED_FILE = DATA_ROOT / "✅ processed.md"

# 索引 / 缓存 / 账本（随数据走，便于整体备份与迁移）
DATA_DIR = DATA_ROOT / ".llm-wiki"
GRAPH_DIR = DATA_DIR
SEARCH_CACHE = DATA_DIR / "search-cache.json"
ZVEC_DB_DIR = DATA_DIR / "zvec_db"
EMBED_CACHE_FILE = DATA_DIR / "embed-cache.json"


# Default metadata files to exclude from wiki page listings
_META_EXCLUDE = {
    "index.md", "log.md", "overview.md", "lint-report.md",
    "health-report.md", "_index.md", "ideas.md",
}

# Minimum content length (excluding frontmatter) to not be considered a stub
STUB_THRESHOLD_CHARS = 100


# ── File I/O ───────────────────────────────────────────────────────────

def read_file(path: Path) -> str:
    """Read file contents as UTF-8. Returns empty string if file doesn't exist."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return ""


def write_file(path: Path, content: str) -> None:
    """Write UTF-8 content to file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def ensure_data_dir() -> Path:
    """Ensure .llm-wiki/ data directory exists, return its path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


# ── Hashing ────────────────────────────────────────────────────────────

def sha256(content: str) -> str:
    """SHA256 hash of string content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ── Frontmatter ────────────────────────────────────────────────────────

def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (--- ... ---) from content."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3:].strip()
    return content.strip()


def extract_frontmatter_field(content: str, field: str) -> str | None:
    """Extract a single field from YAML frontmatter."""
    match = re.search(rf'^{field}:\s*(.+)$', content, re.MULTILINE)
    if match:
        value = match.group(1).strip().strip('"\'')
        return value if value else None
    return None


# ── Wikilinks ──────────────────────────────────────────────────────────

def extract_wikilinks(content: str) -> list[str]:
    """Extract all [[wikilink]] targets from content.

    Handles:
      - [[SimpleLink]]
      - [[Link|Display Text]]
      - [[Link#heading]]

    Returns deduplicated list of link targets (no display text, no heading).
    """
    # Match [[...]] but not ![[]] (image embeds)
    raw = re.findall(r'(?<!!)\[\[([^\]]+)\]', content)
    targets = []
    seen = set()
    for link in raw:
        # Strip display text: [[Link|Display]] → Link
        target = link.split('|')[0].strip()
        # Strip heading: [[Link#heading]] → Link
        target = target.split('#')[0].strip()
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    return targets


# ── Wiki Page Discovery ────────────────────────────────────────────────

def all_wiki_pages() -> list[Path]:
    """Find all .md files under wiki/, raw/ and github-stars/, excluding metadata files.

    ``raw/`` 是原文层（转录稿/推文存档）：进索引是为了能按原话搜到，但检索默认排除
    （见 search.py 的 --include-raw），只作为"原文证据"存在。
    """
    pages = []
    for search_dir in [WIKI_DIR, RAW_DIR, GITHUB_STARS_DIR]:
        if not search_dir.exists():
            continue
        for p in search_dir.rglob("*.md"):
            if p.name in _META_EXCLUDE:
                continue
            pages.append(p)
    return sorted(pages)


def page_id(path: Path) -> str:
    """Convert a wiki page path to a relative ID like 'concepts/注意力经济' or 'github-stars/ai-agents/alibaba-page-agent'.

    raw/ 下的原文返回 'raw/<平台>/<文件名>'。
    """
    for base_dir in [WIKI_DIR, RAW_DIR, GITHUB_STARS_DIR]:
        try:
            rel = str(path.relative_to(base_dir)).replace("\\", "/").replace(".md", "")
        except ValueError:
            continue
        return f"raw/{rel}" if base_dir == RAW_DIR else rel
    return path.stem


def page_name(path: Path) -> str:
    """Get the display name of a wiki page (filename without .md)."""
    return path.stem


# ── CJK Detection ──────────────────────────────────────────────────────

def has_cjk(text: str) -> bool:
    """Check if text contains CJK characters."""
    return any('\u4e00' <= ch <= '\u9fff' for ch in text)


def cjk_bigrams(text: str) -> list[str]:
    """Extract CJK bigrams from text using sliding window.

    Example: "注意力经济" → ["注意", "意力", "力经", "经济"]
    Also includes the full string if it's all CJK and length > 2.
    """
    bigrams = []
    for i in range(len(text) - 1):
        pair = text[i:i+2]
        if any('\u4e00' <= c <= '\u9fff' for c in pair):
            bigrams.append(pair)
    return bigrams


# ── Logging ────────────────────────────────────────────────────────────

def append_log(message: str) -> None:
    """Append a timestamped entry to wiki_log.md."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"- [{timestamp}] {message}\n"
    if LOG_FILE.exists():
        content = read_file(LOG_FILE)
        if not content.endswith("\n"):
            line = "\n" + line
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


# ── Graph Neighbors (for search expansion) ────────────────────────────

def get_neighbors(page_id_str: str, edges: list[dict], min_confidence: float = 0.7) -> set[str]:
    """Find neighbor page IDs from graph edges.

    Includes both EXTRACTED edges (always) and INFERRED edges (if confidence >= min_confidence).
    """
    neighbors = set()
    for edge in edges:
        edge_type = edge.get("type", "EXTRACTED")
        confidence = edge.get("confidence", 1.0)
        include = (edge_type == "EXTRACTED") or (edge_type == "INFERRED" and confidence >= min_confidence)
        if not include:
            continue
        if edge.get("from") == page_id_str:
            neighbors.add(edge["to"])
        elif edge.get("to") == page_id_str:
            neighbors.add(edge["from"])
    return neighbors


# ── Processed File Tracking (SHA256) ───────────────────────────────────

def load_processed() -> dict[str, str]:
    """Load processed file registry as {filename: sha256_hash}.

    Supports both old format (plain filename list) and new format (filename|sha256).
    """
    result = {}
    if not PROCESSED_FILE.exists():
        return result
    content = read_file(PROCESSED_FILE)
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Remove leading "- " list marker
        if stripped.startswith("- "):
            stripped = stripped[2:]
        if not stripped:
            continue
        if ":" in stripped and not stripped.startswith("#"):
            # New format: filename:sha256
            parts = stripped.split(":", 1)
            if len(parts) == 2 and parts[1].strip() and parts[0].strip().endswith(".md"):
                result[parts[0].strip()] = parts[1].strip()
        elif stripped.endswith(".md"):
            # Old format: just a filename (must end in .md to avoid parsing prose)
            result[stripped] = ""
    return result


def is_processed(filename: str, content_hash: str | None = None) -> bool:
    """Check if a file has been processed. If content_hash provided, also verify content unchanged."""
    registry = load_processed()
    if filename not in registry:
        return False
    if content_hash and registry[filename]:
        return registry[filename] == content_hash
    return True  # Old format entry without hash — assume processed


def mark_processed(filename: str, content_hash: str) -> None:
    """Append a file to the processed registry with its SHA256 hash."""
    registry = load_processed()
    registry[filename] = content_hash
    # Rewrite entire file in new format
    lines = ["# Processed Files Log (SHA256)", "# Format: filename:sha256", ""]
    # Group by date prefix if present
    for fname in sorted(registry.keys()):
        h = registry[fname]
        lines.append(f"- {fname}:{h}")
    write_file(PROCESSED_FILE, "\n".join(lines) + "\n")


# ── Main (self-test) ──────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== _utils.py self-test ===")
    print(f"REPO_ROOT: {REPO_ROOT}")
    print(f"WIKI_DIR exists: {WIKI_DIR.exists()}")
    print(f"Wiki pages: {len(all_wiki_pages())}")
    for p in all_wiki_pages()[:5]:
        print(f"  {page_id(p)}")
    print(f"CJK bigrams '注意力经济': {cjk_bigrams('注意力经济')}")
    print(f"Extract wikilinks: {extract_wikilinks('See [[注意力经济]] and [[意义经济|Meaning]] and [[认知Commons#核心]]')}")
    print(f"SHA256 test: {sha256('hello')[:16]}...")
    print("=== All tests passed ===")
