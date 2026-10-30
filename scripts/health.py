#!/usr/bin/env python3
"""
Structural health checks for LLM Wiki.

Purely deterministic — zero LLM calls, fast enough to run every session.
Separate from lint (which includes expensive LLM-powered semantic analysis).

Usage:
    python scripts/health.py              # print report to stdout
    python scripts/health.py --json       # machine-readable output
    python scripts/health.py --save       # also save to wiki/health-report.md
    python scripts/health.py --check category_routing  # run only one check
    python scripts/health.py --files "wiki/summaries/AI & 技术/foo.md"  # incremental mode

Checks:
  - Empty / stub files (pages with no real content beyond frontmatter)
  - Index sync (wiki/index.md entries vs actual files on disk)
  - Log coverage (source pages without a corresponding log entry)
  - Broken wikilinks ([[xxx]] pointing to pages that don't exist)
  - Orphan pages (no inbound wikilinks from other pages)
  - Category routing (YAML category vs actual directory mismatch)

Distilled from llm-wiki-agent (SamurAIGPT) tools/health.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Bootstrap shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from _utils import (
    DATA_ROOT, DATA_DIR, RAW_DIR, WIKI_DIR, INDEX_FILE, LOG_FILE, OVERVIEW_FILE,
    read_file, write_file, strip_frontmatter, extract_wikilinks,
    extract_frontmatter_field,
    all_wiki_pages, page_id, page_name,
    STUB_THRESHOLD_CHARS,
)

# ── Check 1: Empty / Stub Files ────────────────────────────────────────

def check_empty_files(pages: list[Path]) -> list[dict]:
    """Find wiki pages that are empty or contain only frontmatter / minimal content."""
    results = []
    for p in pages:
        raw = read_file(p)
        body = strip_frontmatter(raw)
        if len(body) < STUB_THRESHOLD_CHARS:
            results.append({
                "check": "empty_stub",
                "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                "page_id": page_id(p),
                "body_length": len(body),
                "severity": "warning",
                "message": f"Stub page: only {len(body)} chars of content (threshold: {STUB_THRESHOLD_CHARS})",
            })
    return results


# ── Check 2: Index Sync ────────────────────────────────────────────────

def check_index_sync(pages: list[Path]) -> list[dict]:
    """Check if wiki/index.md entries match actual files on disk."""
    results = []

    if not INDEX_FILE.exists():
        results.append({
            "check": "index_sync",
            "path": "",
            "severity": "error",
            "message": "wiki/index.md does not exist",
        })
        return results

    index_content = read_file(INDEX_FILE)

    # Extract all [[wikilinks]] from index as known entries
    index_links = set(extract_wikilinks(index_content))

    # conversations/（Chat 存档）设计上不进 index.md：仅由总结页/待深挖灵感池通过双链跳转，
    # 不参与 index 主索引（避免每周误报噪音）。
    pages = [p for p in pages if "conversations" not in p.parts]

    # Build set of actual page names
    actual_names = {page_name(p) for p in pages}
    actual_ids = {page_id(p) for p in pages}

    # Pages in index but not on disk
    for link in index_links:
        if link not in actual_names and link not in actual_ids:
            results.append({
                "check": "index_sync",
                "path": "",
                "severity": "warning",
                "message": f"Index references [[{link}]] but no matching page exists",
            })

    # Pages on disk but not in index
    for p in pages:
        name = page_name(p)
        pid = page_id(p)
        if name not in index_links and pid not in index_links:
            # Check if the filename appears anywhere in index content
            if name.lower() not in index_content.lower():
                results.append({
                    "check": "index_sync",
                    "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                    "severity": "info",
                    "message": f"Page '{name}' exists but not referenced in index.md",
                })

    return results


# ── Check 3: Log Coverage ──────────────────────────────────────────────

def check_log_coverage(pages: list[Path]) -> list[dict]:
    """Check if source pages have corresponding log entries."""
    results = []

    if not LOG_FILE.exists():
        results.append({
            "check": "log_coverage",
            "path": "",
            "severity": "warning",
            "message": "wiki_log.md does not exist",
        })
        return results

    log_content = read_file(LOG_FILE)

    # Check source/summary pages for log entries
    for p in pages:
        pid = page_id(p)
        if "summaries/" in pid or "sources/" in pid:
            # Extract a short identifier from the path
            short_id = page_name(p)
            if short_id.lower() not in log_content.lower():
                results.append({
                    "check": "log_coverage",
                    "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                    "severity": "info",
                    "message": f"Source page '{short_id}' has no log entry",
                })

    return results


# ── Check 4: Broken Wikilinks ──────────────────────────────────────────

def check_broken_links(pages: list[Path]) -> list[dict]:
    """Find [[wikilinks]] that point to pages that don't exist.

    raw/ 归档文件也是 Obsidian 可解析的双链目标（原文溯源区指向 raw 的链接
    不应误报为断链），所以把 raw/ 页面并入可解析名称集合。
    """
    results = []
    raw_pages: list[Path] = []
    raw_dir = RAW_DIR
    if raw_dir.exists():
        raw_pages = [p for p in raw_dir.rglob("*.md")]
    known_pages = list(pages) + raw_pages
    actual_names = {page_name(p) for p in known_pages}
    actual_ids = {page_id(p) for p in known_pages}

    for p in pages:
        content = read_file(p)
        links = extract_wikilinks(content)
        for link in links:
            # 带路径双链 [[raw/douyin/xxx]] 的 basename 也要能匹配（page_id 对 raw 返回 stem）
            link_basename = link.split("/")[-1]
            if link not in actual_names and link not in actual_ids and link_basename not in actual_names:
                results.append({
                    "check": "broken_link",
                    "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                    "severity": "warning",
                    "message": f"[[{link}]] points to non-existent page",
                    "target": link,
                })

    return results


# ── Check 5: Orphan Pages ──────────────────────────────────────────────

def check_orphan_pages(pages: list[Path]) -> list[dict]:
    """Find pages with no inbound wikilinks from other pages."""
    results = []
    inbound = defaultdict(int)

    for p in pages:
        content = read_file(p)
        links = extract_wikilinks(content)
        for link in links:
            inbound[link] += 1

    for p in pages:
        name = page_name(p)
        pid = page_id(p)
        if inbound[name] == 0 and inbound[pid] == 0:
            # Skip overview — it's a hub, not an orphan
            if p.name == "overview.md":
                continue
            # 索引型文件（type: topic-index）设计上就是单向汇总，无入链属正常
            # （2026-08-10 孤儿页处理原则：是孤儿就是孤儿，不强行补链）
            content = read_file(p)
            page_type = extract_frontmatter_field(content, "type")
            if page_type == "topic-index":
                continue
            results.append({
                "check": "orphan_page",
                "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                "page_id": pid,
                "severity": "info",
                "message": f"Page '{name}' has no inbound wikilinks (orphan)",
            })

    return results


# ── Check 6: Category Routing ─────────────────────────────────────────

# Preset categories from COMPILE.md (8 core + dynamic expansion allowed)
PRESET_CATEGORIES = {
    "AI & 技术",
    "内容与传播",
    "投资 & 财经",
    "管理 & 领导力",
    "创业",
    "个人成长",
    "生活管理",
    "职业发展",
}

SUMMARIES_DIR = WIKI_DIR / "summaries"


def _replace_frontmatter_field(content: str, field: str, old_value: str, new_value: str) -> str:
    """Replace a YAML frontmatter field value in content.

    Handles both quoted and unquoted values. Returns content unchanged
    if the field/value pair is not found.
    """
    # Match: field: "old_value" or field: 'old_value' or field: old_value
    patterns = [
        rf'(^{field}:\s*)"{re.escape(old_value)}"(\s*$)',
        rf'(^{field}:\s*)\'{re.escape(old_value)}\'(\s*$)',
        rf'(^{field}:\s*){re.escape(old_value)}(\s*$)',
    ]
    for pattern in patterns:
        new_content, count = re.subn(pattern, rf'\1"{new_value}"\2', content, flags=re.MULTILINE)
        if count > 0:
            return new_content
    return content


def _get_existing_category_dirs() -> set[str]:
    """Get all existing subdirectory names under wiki/summaries/."""
    dirs = set()
    if SUMMARIES_DIR.exists():
        for d in SUMMARIES_DIR.iterdir():
            if d.is_dir():
                dirs.add(d.name)
    return dirs


def _get_allowed_categories() -> set[str]:
    """Get the union of preset categories and existing directory names."""
    return PRESET_CATEGORIES | _get_existing_category_dirs()


def _summaries_pages(files: list[Path] | None = None) -> list[Path]:
    """Get summary pages to check. If files given, filter to summaries only."""
    if files is not None:
        # Incremental mode: only check specified files
        return [f for f in files if f.exists() and f.suffix == ".md"]
    # Full scan: all .md under summaries/
    if not SUMMARIES_DIR.exists():
        return []
    return sorted(SUMMARIES_DIR.rglob("*.md"))


def check_category_routing(files: list[Path] | None = None, auto_fix: bool = True) -> list[dict]:
    """Check YAML category field matches actual directory under summaries/.

    Auto-fix: when category != directory name, update YAML to match directory
    (directory is the ground truth — LLM wrote the file there but may have
    put the wrong value in YAML).

    Reports (not auto-fixed):
    - category not in allowed pool (preset + existing dirs)
    - empty category directories
    """
    results = []
    pages = _summaries_pages(files)
    allowed = _get_allowed_categories()
    fixed_count = 0

    for p in pages:
        # Only check files directly under a summaries/ subdirectory
        try:
            rel = p.relative_to(SUMMARIES_DIR)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 2:
            # File directly in summaries/ (no subdirectory) — skip
            continue

        dir_name = parts[0]  # The category directory name
        raw = read_file(p)
        category = extract_frontmatter_field(raw, "category")

        if category is None:
            # No category field — skip (not our concern here)
            continue

        if category != dir_name:
            # Mismatch: auto-fix by updating YAML to match directory
            if auto_fix:
                new_content = _replace_frontmatter_field(raw, "category", category, dir_name)
                if new_content != raw:
                    write_file(p, new_content)
                    fixed_count += 1
                    results.append({
                        "check": "category_routing",
                        "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                        "severity": "info",
                        "message": f"Auto-fixed: category \"{category}\" → \"{dir_name}\" (matched directory)",
                        "auto_fixed": True,
                    })
                else:
                    # Couldn't auto-fix (pattern not found) — report as warning
                    results.append({
                        "check": "category_routing",
                        "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                        "severity": "warning",
                        "message": f"Category mismatch: YAML says \"{category}\" but file is in \"{dir_name}/\" (auto-fix failed)",
                    })
            else:
                results.append({
                    "check": "category_routing",
                    "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                    "severity": "warning",
                    "message": f"Category mismatch: YAML says \"{category}\" but file is in \"{dir_name}/\"",
                })

        # Check if category is in allowed pool (report only, never auto-fix)
        if category not in allowed and category != dir_name:
            # Already reported as mismatch above, skip duplicate
            pass
        elif category not in allowed and category == dir_name:
            # Category matches directory but neither is in allowed pool
            results.append({
                "check": "category_routing",
                "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                "severity": "warning",
                "message": f"Category \"{category}\" not in preset list — confirm this is an intentional new category",
            })

    # Check for empty category directories (only in full scan mode)
    if files is None:
        for d in _get_existing_category_dirs():
            dir_path = SUMMARIES_DIR / d
            md_files = list(dir_path.glob("*.md"))
            if len(md_files) == 0:
                results.append({
                    "check": "category_routing",
                    "path": f"wiki/summaries/{d}",
                    "severity": "info",
                    "message": f"Empty category directory: \"{d}/\" (0 pages)",
                })

    # Summary line for auto-fixes
    if fixed_count > 0:
        results.append({
            "check": "category_routing",
            "path": "",
            "severity": "info",
            "message": f"Auto-fixed {fixed_count} category field(s) to match directory",
            "auto_fixed_count": fixed_count,
        })

    return results


# ── Check 7: Entity Index Consistency ────────────────────────────────

ENTITY_DB_PATH = DATA_DIR / "entity_index.db"


def check_entity_index() -> list[dict]:
    """Check entity index database consistency against actual wiki files.

    Non-destructive — only reads and reports issues.
    """
    results = []

    if not ENTITY_DB_PATH.exists():
        results.append({
            "check": "entity_index",
            "path": "",
            "severity": "info",
            "message": "Entity index database does not exist — run `python scripts/entity_index.py --rebuild` to create",
        })
        return results

    try:
        import sqlite3
        conn = sqlite3.connect(str(ENTITY_DB_PATH))
        conn.row_factory = sqlite3.Row

        # Check 1: Stale chunks (file no longer exists)
        chunks = conn.execute("SELECT page_id, source_path FROM chunks").fetchall()
        stale_count = 0
        for row in chunks:
            full_path = DATA_ROOT / row["source_path"]
            if not full_path.exists():
                stale_count += 1

        if stale_count > 0:
            results.append({
                "check": "entity_index",
                "path": "",
                "severity": "warning",
                "message": f"Entity index has {stale_count} stale chunk(s) pointing to deleted files — run --rebuild to clean up",
            })

        # Check 2: Missing wiki pages (summaries/concepts not in index)
        indexed_page_ids = {row["page_id"] for row in chunks}
        missing_count = 0
        for p in all_wiki_pages():
            pid = page_id(p)
            if "summaries/" in pid or "concepts/" in pid:
                if pid not in indexed_page_ids:
                    missing_count += 1

        if missing_count > 0:
            results.append({
                "check": "entity_index",
                "path": "",
                "severity": "warning",
                "message": f"Entity index missing {missing_count} summary/concept page(s) — run --rebuild to update",
            })

        # Check 3: Entities with mention_count ≤ 0
        zero_count = conn.execute(
            "SELECT COUNT(*) as c FROM entities WHERE mention_count <= 0"
        ).fetchone()["c"]
        if zero_count > 0:
            results.append({
                "check": "entity_index",
                "path": "",
                "severity": "warning",
                "message": f"Entity index has {zero_count} entity(ies) with mention_count ≤ 0 — data integrity issue",
            })

        # Check 4: Orphan entities (no chunk links)
        orphan_count = conn.execute("""
            SELECT COUNT(*) as c FROM entities e
            LEFT JOIN chunk_entities ce ON e.id = ce.entity_id
            WHERE ce.chunk_id IS NULL
        """).fetchone()["c"]
        if orphan_count > 0:
            results.append({
                "check": "entity_index",
                "path": "",
                "severity": "info",
                "message": f"Entity index has {orphan_count} orphan entity(ies) with no chunk links",
            })

        # If no issues, report stats
        if not results:
            chunk_count = conn.execute("SELECT COUNT(*) as c FROM chunks").fetchone()["c"]
            entity_count = conn.execute("SELECT COUNT(*) as c FROM entities").fetchone()["c"]
            link_count = conn.execute("SELECT COUNT(*) as c FROM chunk_entities").fetchone()["c"]
            results.append({
                "check": "entity_index",
                "path": "",
                "severity": "info",
                "message": f"Entity index OK: {chunk_count} chunks, {entity_count} entities, {link_count} links",
            })

        conn.close()

    except Exception as e:
        results.append({
            "check": "entity_index",
            "path": "",
            "severity": "warning",
            "message": f"Entity index check failed: {e}",
        })

    return results


# ── Check 8: Zvec Ghost Index ─────────────────────────────────────────
# 2026-08-10 新增：对比 Zvec 向量库中的 source_path 与磁盘实际文件，
# 找出"索引存在但文件已删除"的幽灵文档（曾因删概念页未清向量索引而残留）。
# 幽灵条目会导致语义搜索返回指向不存在文件的死条目。

def check_zvec_ghosts() -> list[dict]:
    """Check Zvec index for ghost docs (indexed but file deleted on disk)."""
    results = []

    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from zvec_config import (
            get_or_create_collection, FIELD_SOURCE_PATH, FIELD_CONTENT,
        )
        from zvec.model.param.query import Query, Fts

        coll = get_or_create_collection()
        # 用高频词拉取尽可能多的文档（FTS 全量召回），再对每个 source_path 做磁盘存在性检查
        q = Query(field_name=FIELD_CONTENT, fts=Fts(match_string="的"))
        res = coll.query(queries=[q], topk=500)

        disk_paths = set()
        for p in all_wiki_pages():
            if "github-stars" in p.parts:
                continue
            rel = str(p.relative_to(DATA_ROOT)).replace("\\", "/")
            disk_paths.add(rel)

        ghost_paths = set()
        for r in res:
            sp = r.fields.get(FIELD_SOURCE_PATH, "") if hasattr(r, "fields") else ""
            if not sp:
                continue
            if sp.startswith("github-stars"):
                continue
            full = DATA_ROOT / sp
            if not full.exists():
                ghost_paths.add(sp)

        if ghost_paths:
            ghost_list = "、".join(sorted(ghost_paths))
            results.append({
                "check": "zvec_ghost",
                "path": "",
                "severity": "warning",
                "message": f"Zvec index has {len(ghost_paths)} ghost doc(s) (indexed but file deleted): {ghost_list} — run `python scripts/embed.py --rebuild` to clean",
            })
        else:
            results.append({
                "check": "zvec_ghost",
                "path": "",
                "severity": "info",
                "message": "Zvec ghost check OK: no indexed docs pointing to deleted files",
            })

    except Exception as e:
        results.append({
            "check": "zvec_ghost",
            "path": "",
            "severity": "warning",
            "message": f"Zvec ghost check failed: {e}",
        })

    return results


# ── Run All Checks ─────────────────────────────────────────────────────

# Map check names to their functions (for --check flag)
CHECK_FUNCTIONS = {
    "empty_stub": lambda pages: check_empty_files(pages),
    "index_sync": lambda pages: check_index_sync(pages),
    "log_coverage": lambda pages: check_log_coverage(pages),
    "broken_link": lambda pages: check_broken_links(pages),
    "orphan_page": lambda pages: check_orphan_pages(pages),
    "category_routing": lambda pages: check_category_routing(),  # ignores pages param
    "entity_index": lambda pages: check_entity_index(),  # entity index consistency
    "zvec_ghost": lambda pages: check_zvec_ghosts(),  # zvec ghost index check
}


def run_health_check(
    only_check: str | None = None,
    files: list[Path] | None = None,
) -> dict:
    """Run health checks and return a structured report.

    Args:
        only_check: If set, run only this check name (e.g. "category_routing").
        files: If set, run category_routing in incremental mode on these files only.
    """
    pages = all_wiki_pages()
    # github-stars 独立分支（#stars 管理）物理隔离设计使然，不参与主库健康检查（2026-08-03）
    pages = [p for p in pages if "github-stars" not in p.parts]

    all_issues: list[dict] = []

    if only_check:
        if only_check == "category_routing":
            all_issues.extend(check_category_routing(files=files))
        elif only_check in CHECK_FUNCTIONS:
            all_issues.extend(CHECK_FUNCTIONS[only_check](pages))
        else:
            return {
                "status": "error",
                "total_pages": len(pages),
                "total_issues": 1,
                "errors": 1, "warnings": 0, "infos": 0,
                "issues": [{"check": "cli", "severity": "error",
                            "message": f"Unknown check: {only_check}. Available: {', '.join(CHECK_FUNCTIONS.keys())}"}],
            }
    else:
        all_issues.extend(check_empty_files(pages))
        all_issues.extend(check_index_sync(pages))
        all_issues.extend(check_log_coverage(pages))
        all_issues.extend(check_broken_links(pages))
        all_issues.extend(check_orphan_pages(pages))
        all_issues.extend(check_category_routing())

    # Entity index consistency (always run, non-destructive)
    # 例外：--check entity_index 时上面 CHECK_FUNCTIONS 已经跑过，避免同一问题报两遍
    if only_check != "entity_index":
        all_issues.extend(check_entity_index())

    # Zvec ghost index check (always run, non-destructive)
    # 例外同上（--check zvec_ghost）
    if only_check != "zvec_ghost":
        all_issues.extend(check_zvec_ghosts())

    # Summary
    errors = sum(1 for i in all_issues if i["severity"] == "error")
    warnings = sum(1 for i in all_issues if i["severity"] == "warning")
    infos = sum(1 for i in all_issues if i["severity"] == "info")

    return {
        "status": "error" if errors > 0 else ("warning" if warnings > 0 else "ok"),
        "total_pages": len(pages),
        "total_issues": len(all_issues),
        "errors": errors,
        "warnings": warnings,
        "infos": infos,
        "issues": all_issues,
    }


def format_report(report: dict) -> str:
    """Format health report as human-readable text."""
    lines = [
        f"=== LLM Wiki Health Check ===",
        f"Status: {report['status'].upper()}",
        f"Pages: {report['total_pages']}",
        f"Issues: {report['total_issues']} ({report['errors']} errors, {report['warnings']} warnings, {report['infos']} info)",
        "",
    ]

    if not report["issues"]:
        lines.append("✅ All checks passed!")
        return "\n".join(lines)

    # Group by check type
    by_check = defaultdict(list)
    for issue in report["issues"]:
        by_check[issue["check"]].append(issue)

    for check_name, issues in by_check.items():
        lines.append(f"── {check_name} ({len(issues)} issues) ──")
        for issue in issues:
            icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(issue["severity"], "•")
            lines.append(f"  {icon} {issue['message']}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="LLM Wiki Health Check (deterministic, 0 LLM calls)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--save", action="store_true", help="Save report to wiki/health-report.md")
    parser.add_argument("--check", type=str, default=None,
                        help="Run only one check (e.g. category_routing)")
    parser.add_argument("--files", type=str, nargs="*", default=None,
                        help="Specific files to check (incremental mode for category_routing)")
    args = parser.parse_args()

    # 空库友好提示：用户还没跑初始化向导时，别甩一堆 ERROR 吓人
    if not (DATA_ROOT / "00-INBOX").exists() and not (DATA_ROOT / "wiki").exists():
        if args.json:
            # --json 模式下 stdout 必须恒为合法 JSON，否则下游消费会静默拿到脏数据
            print(json.dumps({
                "status": "uninitialized",
                "data_root": str(DATA_ROOT),
                "pages": 0,
                "issues": [],
                "hint": "python setup.py   （或 install.ps1 / install.sh）",
            }, ensure_ascii=False))
            return 0
        print("知识库尚未初始化（数据目录里还没有骨架）。")
        print(f"  数据目录：{DATA_ROOT}")
        print("  请先运行初始化向导：python setup.py   （或 install.ps1 / install.sh）")
        print("  说明：本项目不依赖 Obsidian —— 数据目录就是个普通文件夹，")
        print("        骨架由初始化向导自动创建（幂等，不会覆盖已有内容）。")
        return 0

    # Parse --files into Path objects
    file_paths = None
    if args.files:
        file_paths = [DATA_ROOT / f for f in args.files]

    report = run_health_check(only_check=args.check, files=file_paths)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))

    if args.save:
        report_path = WIKI_DIR / "health-report.md"
        content = f"# Health Report\n\nGenerated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n```\n{format_report(report)}\n```\n"
        write_file(report_path, content)
        print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    # Windows 管道/控制台默认 GBK：emoji 会让 print 崩掉（工作其实已完成）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
