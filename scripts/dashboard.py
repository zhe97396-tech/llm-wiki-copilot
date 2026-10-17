#!/usr/bin/env python3
"""
Dashboard Generator for LLM Wiki.

Reads knowledge base state and generates a static HTML dashboard.
Run: python scripts/dashboard.py
Output: index/dashboard.html (double-click to open)
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Bootstrap shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from _utils import (
    WIKI_DIR, PROCESSED_FILE, DATA_ROOT, DATA_DIR, INBOX_DIR, LOG_FILE, INDEX_DIR,
    read_file, all_wiki_pages, page_id, page_name,
    extract_frontmatter_field, strip_frontmatter,
    has_cjk, cjk_bigrams, extract_wikilinks,
    sha256, ensure_data_dir,
)


def scan_knowledge_base() -> dict:
    """Scan the entire knowledge base and return structured stats."""
    pages = all_wiki_pages()

    # Categorize pages
    summaries = []  # wiki/summaries/
    concepts = []   # wiki/concepts/

    for p in pages:
        content = read_file(p)
        pid = page_id(p)
        name = page_name(p)
        status = extract_frontmatter_field(content, "status") or "unknown"
        category = extract_frontmatter_field(content, "category") or ""
        ptype = extract_frontmatter_field(content, "type") or "source"
        date = extract_frontmatter_field(content, "date") or ""
        title = extract_frontmatter_field(content, "title") or name

        entry = {
            "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
            "name": name,
            "title": title,
            "status": status,
            "category": category,
            "type": ptype,
            "date": date,
        }

        if "summaries/" in pid:
            summaries.append(entry)
        elif "concepts/" in pid:
            concepts.append(entry)

    # Count by status
    status_counts = defaultdict(int)
    for s in summaries:
        status_counts[s["status"]] += 1

    # Count concepts by category
    concept_categories = defaultdict(list)
    for c in concepts:
        cat = c.get("category", "未分类")
        concept_categories[cat].append(c["title"])

    # Find orphans (concepts not linked from any summary)
    all_wikilinks = set()
    for p in pages:
        content = read_file(p)
        links = extract_wikilinks(content)
        all_wikilinks.update(links)

    orphans = []
    for c in concepts:
        name = c["name"]
        if name not in all_wikilinks and c["title"] not in all_wikilinks:
            orphans.append(c["title"])

    # Recent summaries (by date)
    recent = sorted(summaries, key=lambda x: x.get("date", ""), reverse=True)[:10]

    # 创作脚本：开源版不含创作模块
    script_count = 0

    # 待深挖灵感池：wiki/ideas.md 的行数
    ideas_file = WIKI_DIR / "ideas.md"
    topic_count = len(ideas_file.read_text(encoding="utf-8").split("\n")) if ideas_file.exists() else 0

    # INBOX count
    inbox_dir = INBOX_DIR
    inbox_count = len(list(inbox_dir.glob("*.md"))) if inbox_dir.exists() else 0

    # Stale articles (compiled/unread > 30 days)
    from datetime import timedelta
    stale_cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    stale = [s for s in summaries if s["status"] in ("compiled", "unread") and s.get("date", "") < stale_cutoff]

    # Evergreen articles (status == "evergreen")
    evergreen = [s for s in summaries if s["status"] == "evergreen"]

    # Category distribution (summaries by category)
    category_counts = defaultdict(int)
    for s in summaries:
        cat = s.get("category", "") or "未分类"
        category_counts[cat] += 1

    # Conversations count
    conversations_dir = WIKI_DIR / "conversations"
    conversation_files = sorted(conversations_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if conversations_dir.exists() else []
    conversation_count = len(conversation_files)
    recent_conversations = []
    for cf in conversation_files[:5]:
        content = read_file(cf)
        title = extract_frontmatter_field(content, "title") or cf.stem
        date = extract_frontmatter_field(content, "date") or ""
        recent_conversations.append({"title": title, "date": date})

    # RAG index status
    zvec_dir = DATA_DIR / "zvec_db"
    zvec_exists = zvec_dir.exists()
    zvec_doc_count = 0
    if zvec_exists:
        for f in zvec_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    zvec_doc_count += len(data)
                elif isinstance(data, dict) and "documents" in data:
                    zvec_doc_count += len(data["documents"])
            except Exception:
                pass

    # Operation timeline (from wiki_log.md, last 10 entries)
    log_file = LOG_FILE
    log_entries = []
    auto_evolution = []  # Recent auto-evolution entries
    if log_file.exists():
        log_content = read_file(log_file)
        for line in log_content.strip().split("\n"):
            line = line.strip()
            if line.startswith("- ["):
                entry = line.lstrip("- ").strip()
                log_entries.append(entry)
                if "🔄 自动进化" in line:
                    auto_evolution.append(entry)
    recent_log = log_entries[-10:] if log_entries else []
    recent_evolution = auto_evolution[-10:] if auto_evolution else []

    return {
        "total_concepts": len(concepts),
        "total_summaries": len(summaries),
        "status_counts": dict(status_counts),
        "concept_categories": {k: v for k, v in concept_categories.items()},
        "orphans": orphans,
        "recent_summaries": recent,
        "stale_articles": stale,
        "evergreen_articles": evergreen,
        "script_count": script_count,
        "topic_count": topic_count,
        "inbox_count": inbox_count,
        "category_counts": dict(category_counts),
        "conversation_count": conversation_count,
        "recent_conversations": recent_conversations,
        "zvec_exists": zvec_exists,
        "zvec_doc_count": zvec_doc_count,
        "recent_log": recent_log,
        "recent_evolution": recent_evolution,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def run_health_check() -> list[dict]:
    """Run health check and return issues."""
    from health import run_health_check as _run_health
    report = _run_health()
    return report.get("issues", [])


def generate_html(stats: dict, issues: list[dict]) -> str:
    """Generate the dashboard HTML — Linear-style dark theme."""

    # ── Design tokens (Linear recipe) ──────────────────────────────
    # Ground: #08090A  Surface1: #16171C  Surface2: #1E1F25  Surface3: #26272E
    # Hairline: rgba(255,255,255,0.06)  Accent: #5E6AD2
    # Primary text: #F7F8F8  Secondary: #9CA3AF  Muted: #6B7280
    # Mono: code chips  Radius: 6/12  No emoji  No shadow glow

    # Status colors — warm palette
    status_colors = {
        "compiled": "#C2762E",   # amber accent
        "reading": "#2E86AB",    # teal blue
        "distilled": "#7B5EA7",  # muted purple
        "archived": "#9C8E80",   # warm gray
        "unread": "#6B5E52",     # secondary
        "unknown": "#9C8E80",
    }

    status_labels = {
        "compiled": "已编译",
        "reading": "带读中",
        "distilled": "已沉淀",
        "archived": "已归档",
        "unread": "待处理",
        "unknown": "未知",
    }

    # ── Status cards ───────────────────────────────────────────────
    status_cards = ""
    for status, count in sorted(stats["status_counts"].items(), key=lambda x: -x[1]):
        color = status_colors.get(status, "#6B7280")
        label = status_labels.get(status, status)
        status_cards += f"""
        <div class="status-card">
            <div class="status-dot" style="background:{color}"></div>
            <div class="status-count" style="color:{color}">{count}</div>
            <div class="status-name">{label}</div>
        </div>"""

    # ── Concept tags ───────────────────────────────────────────────
    concept_cats = ""
    for cat, items in sorted(stats["concept_categories"].items()):
        cat_label = cat if cat else "未分类"
        concept_cats += f"""
        <div class="concept-section">
            <div class="concept-section-label">{cat_label}<span class="count">{len(items)}</span></div>
            <div class="concept-chips">{"".join(f'<span class="chip">{i}</span>' for i in items)}</div>
        </div>"""

    # ── Recent list ────────────────────────────────────────────────
    recent_html = ""
    for r in stats["recent_summaries"]:
        color = status_colors.get(r["status"], "#6B7280")
        label = status_labels.get(r["status"], r["status"])
        recent_html += f"""
        <div class="recent-row">
            <div class="recent-indicator" style="background:{color}"></div>
            <div class="recent-info">
                <div class="recent-title">{r['title']}</div>
                <div class="recent-meta"><span class="chip-sm" style="color:{color};border-color:{color}33">{label}</span> {r['date']}</div>
            </div>
        </div>"""

    # ── Orphans ────────────────────────────────────────────────────
    orphans_html = ""
    if stats["orphans"]:
        for o in stats["orphans"]:
            orphans_html += f'<div class="orphan-row"><div class="orphan-dot"></div>{o}</div>'
    else:
        orphans_html = '<div class="empty-state">无隐含关联建议</div>'

    # ── Health issues ──────────────────────────────────────────────
    severity_map = {"error": ("ERR", "#C0392B"), "warning": ("WARN", "#C2762E"), "info": ("INFO", "#2E86AB")}
    health_html = ""
    if issues:
        for issue in issues:
            sev = issue.get("severity", "info")
            label, color = severity_map.get(sev, ("INFO", "#5E6AD2"))
            health_html += f"""
            <div class="health-row">
                <span class="health-badge" style="color:{color};border-color:{color}40">{label}</span>
                <span class="health-msg">{issue.get('message', '')}</span>
            </div>"""
    else:
        health_html = '<div class="empty-state">全部检查通过</div>'

    # ── KPI bar ────────────────────────────────────────────────────
    kpis = [
        ("待入库", stats["inbox_count"]),
        ("概念", stats["total_concepts"]),
        ("总结", stats["total_summaries"]),
        ("脚本", stats["script_count"]),
        ("灵感池", stats["topic_count"]),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-value">{v}</div><div class="kpi-label">{l}</div></div>'
        for l, v in kpis
    )

    # ── Stale articles ────────────────────────────────────────────
    if stats["stale_articles"]:
        stale_html = ""
        for s in stats["stale_articles"]:
            stale_html += f"""<div class="stale-row"><span class="stale-title">{s['title']}</span><span class="stale-date">{s['date']}</span></div>"""
    else:
        stale_html = '<div class="empty-state">无过期文章</div>'

    # ── Evergreen ─────────────────────────────────────────────────
    if stats["evergreen_articles"]:
        evergreen_html = ""
        for e in stats["evergreen_articles"]:
            evergreen_html += f"""<div class="evergreen-row">{e['title']}</div>"""
    else:
        evergreen_html = '<div class="empty-state">暂无常青笔记</div>'

    # ── Category distribution ────────────────────────────────────
    cat_html = ""
    if stats["category_counts"]:
        max_cat = max(stats["category_counts"].values())
        for cat, count in sorted(stats["category_counts"].items(), key=lambda x: -x[1]):
            cat_label = cat if cat else "未分类"
            width_pct = int(count / max_cat * 100) if max_cat else 0
            cat_html += f"""<div class="cat-row"><span class="cat-label">{cat_label}</span><div class="cat-bar-track"><div class="cat-bar-fill" style="width:{width_pct}%"></div></div><span class="cat-count">{count}</span></div>"""
    else:
        cat_html = '<div class="empty-state">暂无分类数据</div>'

    # ── Conversations ─────────────────────────────────────────────
    conv_html = ""
    if stats["recent_conversations"]:
        for c in stats["recent_conversations"]:
            conv_html += f"""<div class="conv-row"><span class="conv-title">{c['title']}</span><span class="conv-date">{c['date']}</span></div>"""
    else:
        conv_html = '<div class="empty-state">暂无复盘记录</div>'

    # ── RAG index status ──────────────────────────────────────────
    rag_color = "#27AE60" if stats["zvec_exists"] else "#C0392B"
    rag_label = "正常" if stats["zvec_exists"] else "未初始化"
    rag_html = f"""<div class="rag-row"><div class="rag-dot" style="background:{rag_color}"></div><span class="rag-label">索引状态</span><span class="rag-value">{rag_label}</span></div>"""
    rag_html += f"""<div class="rag-row"><div class="rag-dot" style="background:transparent"></div><span class="rag-label">已索引文档</span><span class="rag-value">{stats['zvec_doc_count']}</span></div>"""
    rag_html += f"""<div class="rag-row"><div class="rag-dot" style="background:transparent"></div><span class="rag-label">wiki页面总数</span><span class="rag-value">{stats['total_concepts'] + stats['total_summaries']}</span></div>"""

    # ── Auto-evolution activity ──────────────────────────────────────
    evolution_html = ""
    if stats.get("recent_evolution"):
        for entry in reversed(stats["recent_evolution"]):
            entry_safe = entry.replace("<", "&lt;").replace(">", "&gt;")
            evolution_html += f"""<div class="timeline-row">{entry_safe}</div>"""
    else:
        evolution_html = '<div class="empty-state">暂无自动进化记录</div>'

    # ── Operation timeline ────────────────────────────────────────
    timeline_html = ""
    if stats["recent_log"]:
        for entry in reversed(stats["recent_log"]):
            # Escape HTML
            entry_safe = entry.replace("<", "&lt;").replace(">", "&gt;")
            timeline_html += f"""<div class="timeline-row">{entry_safe}</div>"""
    else:
        timeline_html = '<div class="empty-state">暂无操作记录</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM Wiki</title>
<style>
  :root {{
    --ground: #FAF8F5;
    --s1: #FFFFFF;
    --s2: #F5F2EE;
    --s3: #EBE7E2;
    --hairline: rgba(0,0,0,0.08);
    --accent: #C2762E;
    --accent-light: #F5E6D3;
    --text-1: #2C2520;
    --text-2: #6B5E52;
    --text-3: #9C8E80;
    --mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;
    --sans: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
    --radius-sm: 6px;
    --radius-md: 12px;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: var(--sans);
    background: var(--ground);
    color: var(--text-2);
    font-size: 14px;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}
  .shell {{
    max-width: 1400px;
    margin: 0 auto;
    padding: 40px 32px 80px;
  }}

  /* ── Header ──────────────────────────────────────────────── */
  .header {{
    margin-bottom: 48px;
  }}
  .header-title {{
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-3);
    margin-bottom: 4px;
  }}
  .header-sub {{
    font-size: 12px;
    color: var(--text-3);
    font-family: var(--mono);
  }}
  .header-sub code {{
    color: #C2762E;
    background: var(--s2);
    padding: 2px 6px;
    border-radius: var(--radius-sm);
    font-size: 11px;
  }}

  /* ── KPI strip ───────────────────────────────────────────── */
  .kpi-strip {{
    display: flex;
    gap: 1px;
    background: var(--hairline);
    border-radius: var(--radius-md);
    overflow: hidden;
    margin-bottom: 40px;
  }}
  .kpi {{
    flex: 1;
    background: var(--s1);
    padding: 20px 16px;
    text-align: center;
  }}
  .kpi-value {{
    font-size: 28px;
    font-weight: 600;
    color: var(--text-1);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }}
  .kpi-label {{
    font-size: 11px;
    color: var(--text-3);
    margin-top: 4px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}

  /* ── Section ─────────────────────────────────────────────── */
  .section {{
    margin-bottom: 32px;
  }}
  .section-head {{
    font-size: 12px;
    font-weight: 500;
    color: var(--text-3);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--hairline);
    margin-bottom: 16px;
  }}

  /* ── Status cards ────────────────────────────────────────── */
  .status-row {{
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .status-card {{
    background: var(--s1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius-sm);
    padding: 14px 18px;
    min-width: 130px;
    flex: 1;
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .status-dot {{
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }}
  .status-count {{
    font-size: 20px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }}
  .status-name {{
    font-size: 12px;
    color: var(--text-3);
    margin-left: auto;
  }}

  /* ── Three-col grid ───────────────────────────────────────── */
  .grid-2 {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 24px;
  }}
  @media (max-width: 900px) {{ .grid-2 {{ grid-template-columns: 1fr 1fr; }} }}
  @media (max-width: 640px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}

  /* ── Concept chips ───────────────────────────────────────── */
  .concept-section {{ margin-bottom: 12px; }}
  .concept-section-label {{
    font-size: 11px;
    color: var(--text-3);
    margin-bottom: 8px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .concept-section-label .count {{
    margin-left: 6px;
    color: var(--text-3);
    font-family: var(--mono);
    font-size: 10px;
  }}
  .concept-chips {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }}
  .chip {{
    background: var(--s2);
    border: 1px solid var(--hairline);
    color: var(--text-2);
    padding: 3px 10px;
    border-radius: var(--radius-sm);
    font-size: 12px;
    font-family: var(--mono);
  }}

  /* ── Recent list ─────────────────────────────────────────── */
  .recent-row {{
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 0;
    border-bottom: 1px solid var(--hairline);
  }}
  .recent-row:last-child {{ border-bottom: none; }}
  .recent-indicator {{
    width: 3px;
    height: 32px;
    border-radius: 2px;
    flex-shrink: 0;
    margin-top: 2px;
  }}
  .recent-info {{ flex: 1; min-width: 0; }}
  .recent-title {{
    font-size: 13px;
    color: var(--text-1);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .recent-meta {{
    font-size: 11px;
    color: var(--text-3);
    margin-top: 3px;
    display: flex;
    align-items: center;
    gap: 6px;
  }}
  .chip-sm {{
    font-size: 10px;
    font-family: var(--mono);
    border: 1px solid;
    padding: 1px 5px;
    border-radius: 3px;
  }}

  /* ── Orphans ─────────────────────────────────────────────── */
  .orphan-row {{
    padding: 6px 0;
    font-size: 13px;
    color: var(--text-2);
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .orphan-dot {{
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: #C0392B;
    flex-shrink: 0;
  }}

  /* ── Health ──────────────────────────────────────────────── */
  .health-row {{
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 8px 0;
    border-bottom: 1px solid var(--hairline);
  }}
  .health-row:last-child {{ border-bottom: none; }}
  .health-badge {{
    font-size: 10px;
    font-family: var(--mono);
    border: 1px solid;
    padding: 1px 5px;
    border-radius: 3px;
    flex-shrink: 0;
    margin-top: 1px;
  }}
  .health-msg {{
    font-size: 12px;
    color: var(--text-2);
    line-height: 1.5;
  }}

  /* ── Empty state ─────────────────────────────────────────── */
  .empty-state {{
    font-size: 12px;
    color: var(--text-3);
    padding: 12px 0;
  }}

  /* ── Panel (card replacement) ────────────────────────────── */
  .panel {{
    background: var(--s1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius-md);
    padding: 20px;
  }}

  /* ── Stale ─────────────────────────────────────────────────── */
  .stale-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
    border-bottom: 1px solid var(--hairline);
    font-size: 13px;
  }}
  .stale-row:last-child {{ border-bottom: none; }}
  .stale-title {{ color: var(--text-2); }}
  .stale-date {{ color: var(--text-3); font-size: 11px; font-family: var(--mono); white-space: nowrap; }}

  /* ── Evergreen ──────────────────────────────────────────────── */
  .evergreen-row {{
    padding: 6px 0;
    font-size: 13px;
    color: var(--text-1);
    border-bottom: 1px solid var(--hairline);
  }}
  .evergreen-row:last-child {{ border-bottom: none; }}

  /* ── Category bar ──────────────────────────────────────────── */
  .cat-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 0;
    font-size: 12px;
  }}
  .cat-label {{
    color: var(--text-2);
    min-width: 80px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .cat-bar-track {{
    flex: 1;
    height: 6px;
    background: var(--s3);
    border-radius: 3px;
    overflow: hidden;
  }}
  .cat-bar-fill {{
    height: 100%;
    background: var(--accent);
    border-radius: 3px;
  }}
  .cat-count {{
    color: var(--text-3);
    font-family: var(--mono);
    font-size: 10px;
    min-width: 20px;
    text-align: right;
  }}

  /* ── Conversation row ──────────────────────────────────────── */
  .conv-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
    border-bottom: 1px solid var(--hairline);
    font-size: 13px;
  }}
  .conv-row:last-child {{ border-bottom: none; }}
  .conv-title {{ color: var(--text-2); }}
  .conv-date {{ color: var(--text-3); font-size: 11px; font-family: var(--mono); white-space: nowrap; }}

  /* ── RAG status ────────────────────────────────────────────── */
  .rag-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 0;
    font-size: 12px;
  }}
  .rag-label {{ color: var(--text-3); }}
  .rag-value {{ color: var(--text-1); font-weight: 600; font-family: var(--mono); }}
  .rag-dot {{
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }}

  /* ── Timeline ──────────────────────────────────────────────── */
  .timeline-row {{
    padding: 4px 0;
    font-size: 12px;
    color: var(--text-2);
    border-bottom: 1px solid var(--hairline);
    line-height: 1.5;
  }}
  .timeline-row:last-child {{ border-bottom: none; }}
</style>
</head>
<body>
<div class="shell">

  <div class="header">
    <div class="header-title">LLM Wiki 知识看板</div>
    <div class="header-sub">更新于 {stats['generated_at']} &middot; <code>python scripts/dashboard.py</code></div>
  </div>

  <div class="kpi-strip">{kpi_html}</div>

  <div class="section">
    <div class="section-head">状态</div>
    <div class="status-row">{status_cards}</div>
  </div>

  <div class="grid-2">
    <div class="section">
      <div class="section-head">概念</div>
      <div class="panel">{concept_cats}</div>
    </div>

    <div class="section">
      <div class="section-head">最近入库</div>
      <div class="panel">{recent_html}</div>
    </div>

    <div class="section">
      <div class="section-head">分类分布</div>
      <div class="panel">{cat_html}</div>
    </div>

    <div class="section">
      <div class="section-head">隐含关联</div>
      <div class="panel">{orphans_html}</div>
    </div>

    <div class="section">
      <div class="section-head">健康</div>
      <div class="panel">{health_html}</div>
    </div>

    <div class="section">
      <div class="section-head">复盘记录</div>
      <div class="panel">{conv_html}</div>
    </div>

    <div class="section">
      <div class="section-head">过期 &middot; &gt;30天</div>
      <div class="panel">{stale_html}</div>
    </div>

    <div class="section">
      <div class="section-head">常青</div>
      <div class="panel">{evergreen_html}</div>
    </div>

    <div class="section">
      <div class="section-head">🔄 自动进化</div>
      <div class="panel">{evolution_html}</div>
    </div>

    <div class="section">
      <div class="section-head">RAG 索引</div>
      <div class="panel">{rag_html}</div>
    </div>

    <div class="section">
      <div class="section-head">操作时间线</div>
      <div class="panel">{timeline_html}</div>
    </div>
  </div>

</div>
</body>
</html>"""


def main():
    print("📊 Generating dashboard...")
    stats = scan_knowledge_base()
    issues = run_health_check()
    html = generate_html(stats, issues)

    output_path = INDEX_DIR / "dashboard.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"✅ Dashboard generated: {output_path}")
    print(f"   {stats['total_concepts']} concepts, {stats['total_summaries']} summaries")
    print(f"   {len(issues)} health issues")


if __name__ == "__main__":
    main()
