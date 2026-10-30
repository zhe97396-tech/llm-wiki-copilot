#!/usr/bin/env python3
"""
Ask Engine for LLM Wiki: retrieval + complexity classification + coverage scoring + gap detection.

Replaces the old "search Top10 → answer" flow with an intent-aware pipeline:
  1. Retrieve from knowledge base (dual-path + P0-P3 optimizations)
  2. Classify question complexity (simple / complex / comparative)
  3. Score knowledge base coverage (0-1)
  4. Detect gap topics (uncovered keywords)
  5. Output structured JSON for the prompt layer

Usage:
    python scripts/ask.py "注意力经济是什么"
    python scripts/ask.py "怎么建立阅读习惯" --top 20
    python scripts/ask.py "怎么建立阅读习惯" --classify-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Bootstrap shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from _utils import (
    WIKI_DIR, all_wiki_pages, page_id, page_name,
    read_file, strip_frontmatter, extract_frontmatter_field,
)
from search import search, similar_docs

# ── Complexity Classification ────────────────────────────────────────

# Keywords that signal a comparative question
_COMPARATIVE_KEYWORDS = {
    "vs", "还是", "区别", "关系", "联系", "对比", "比较", "不同",
    "差异", "相同", "共同", "优劣", "哪个好", "哪个更适合",
}

# Keywords that signal a complex/planning question
_COMPLEX_KEYWORDS = {
    "怎么做", "如何开始", "步骤", "路线", "规划", "实操",
    "入门", "怎么开始", "怎么做", "如何做", "流程", "路径",
    "方法论", "框架", "体系", "全景", "全貌", "梳理",
    "整合", "总结成", "整理成", "帮我列", "都列出来",
    # Implicit complex: user wants to DO something (planning/execution intent)
    "我想做", "我想学", "我想开始", "我要做", "我要学",
    "想做", "想学", "要做", "要学", "怎么搞", "如何入手",
    "整理", "梳理", "汇总", "归纳", "全列出来",
}

# Minimum thresholds for complex classification
_MIN_CONCEPTS_FOR_COMPLEX = 5  # High threshold — many simple questions hit 3+ concept pages

# Keywords that signal a CLOSED/simple question (not complex even with many results)
_SIMPLE_QUESTION_SUFFIXES = {
    "是什么", "是什么意思", "是什么概念", "定义", "含义",
    "是什么意思", "指什么", "什么意思", "怎么理解",
    "是什么", "是什么东西", "是啥", "啥是",
}


def _extract_query_keywords(query: str) -> list[str]:
    """Extract meaningful keywords from query via jieba."""
    try:
        import jieba
        return [w for w in jieba.cut(query) if len(w) >= 2]
    except ImportError:
        return [w for w in query.split() if len(w) >= 2]


def classify_complexity(
    query: str,
    results: list[dict],
) -> str:
    """Classify question complexity based on query text and retrieval results.

    Returns: "comparative" | "complex" | "simple"

    Strategy: Query text is the PRIMARY signal. Retrieval results are
    SECONDARY — used only to confirm comparative/complex when query
    keywords alone are ambiguous.

    Priority: comparative > complex > simple (first match wins).
    """
    query_lower = query.lower()

    # 0. Pre-check: if query is a closed/simple question, return simple immediately
    # This overrides all other signals — "X是什么" is always simple
    if any(query_lower.endswith(sfx) or sfx in query_lower for sfx in _SIMPLE_QUESTION_SUFFIXES):
        return "simple"

    # 1. COMPARATIVE: query contains comparison keywords + results span multiple concepts
    has_comparative_kw = any(kw in query_lower for kw in _COMPARATIVE_KEYWORDS)
    if has_comparative_kw:
        # Check if results mention different concepts
        concept_titles = set()
        for r in results:
            if r.get("type") in ("concept", "entity") or "concepts/" in r.get("path", ""):
                concept_titles.add(r.get("title", ""))
        if len(concept_titles) >= 2:
            return "comparative"
        # Even with only 1 concept found, if user explicitly asked for comparison, it's comparative
        if has_comparative_kw and len(concept_titles) >= 1:
            return "comparative"

    # 2. COMPLEX: primary signal is query keywords, secondary is result diversity
    has_complex_kw = any(kw in query_lower for kw in _COMPLEX_KEYWORDS)
    if has_complex_kw:
        return "complex"

    # Check result diversity as secondary signal for complex
    # Only count DIRECT Zvec hits (not expanded/fallback) that are concept pages
    direct_concepts = set()
    for r in results:
        if (not r.get("expanded_from")
            and r.get("backend") not in ("keyword_fallback", "entity_bridge")
            and (r.get("type") in ("concept", "entity") or "concepts/" in r.get("path", ""))):
            direct_concepts.add(r.get("title", ""))

    # If 3+ different concept pages directly matched, this is likely a complex topic
    if len(direct_concepts) >= _MIN_CONCEPTS_FOR_COMPLEX:
        return "complex"

    # 3. SIMPLE: default
    return "simple"


# ── Coverage Scoring ─────────────────────────────────────────────────

def compute_coverage(
    query: str,
    results: list[dict],
    keywords: list[str],
) -> tuple[float, str]:
    """Compute knowledge base coverage score.

    Returns: (score, level)
      - score: 0.0 - 1.0
      - level: "complete" | "partial" | "insufficient"
    """
    if not results:
        return (0.0, "insufficient")

    # Factor 1: high-confidence direct hit density (exclude supplementary sources)
    high_conf = sum(
        1 for r in results
        if r.get("confidence") == "high"
        and r.get("backend") not in ("keyword_fallback", "entity_bridge")
        and not r.get("expanded_from")
    )
    density = min(1.0, high_conf / 3.0)

    # Factor 2: keyword coverage — what fraction of query keywords appear in results
    if keywords:
        all_snippets = " ".join(r.get("snippet", "").lower() for r in results)
        all_titles = " ".join(r.get("title", "").lower() for r in results)
        combined = all_snippets + " " + all_titles
        covered = sum(1 for kw in keywords if kw.lower() in combined)
        topic_coverage = covered / len(keywords)
    else:
        topic_coverage = 0.5  # No keywords to check, assume partial

    coverage = density * topic_coverage

    if coverage >= 0.7:
        level = "complete"
    elif coverage >= 0.4:
        level = "partial"
    else:
        level = "insufficient"

    return (round(coverage, 3), level)


# ── Gap Detection ────────────────────────────────────────────────────

def detect_gaps(
    keywords: list[str],
    results: list[dict],
) -> list[str]:
    """Find query keywords not covered by any result."""
    if not keywords or not results:
        return keywords if keywords else []

    all_text = " ".join(
        r.get("snippet", "").lower() + " " + r.get("title", "").lower()
        for r in results
    )

    gaps = []
    for kw in keywords:
        if kw.lower() not in all_text:
            gaps.append(kw)

    return gaps


# ── Main Ask Engine ──────────────────────────────────────────────────

def ask(
    query: str,
    top_k: int | None = None,
    backend: str = "zvec",
    exclude_types: set[str] | None = None,
) -> dict:
    """Run the full Ask pipeline: retrieve → classify → coverage → gaps.

    Args:
        query: User's question.
        top_k: Override result count. None = auto (simple=5, complex=20).
        backend: Search backend ("zvec" or "bigram").
        exclude_types: Page types to exclude.

    Returns:
        Structured dict with complexity, coverage, results, etc.
    """
    keywords = _extract_query_keywords(query)

    # Step 1: Initial retrieval with moderate depth to classify
    initial_top = top_k or 10
    results = search(query, top_k=initial_top, backend=backend, exclude_types=exclude_types)

    # Step 2: Classify complexity
    complexity = classify_complexity(query, results)

    # Step 3: If complex and we used default top_k, re-retrieve with more depth
    if complexity == "complex" and top_k is None and len(results) < 15:
        results = search(query, top_k=20, backend=backend, exclude_types=exclude_types)
        # Re-classify with expanded results (complexity may change)
        complexity = classify_complexity(query, results)

    # Step 4: Compute coverage
    coverage, coverage_level = compute_coverage(query, results, keywords)

    # Step 5: Detect gaps
    gap_topics = detect_gaps(keywords, results)

    # Step 6: Collect stats
    high_conf = sum(1 for r in results if r.get("confidence") == "high")
    dirs = set()
    concept_pages = set()
    for r in results:
        path = r.get("path", "")
        parts = path.rsplit("/", 1)
        dirs.add(parts[0] if len(parts) > 1 else "_root")
        if r.get("type") in ("concept", "entity") or "concepts/" in path:
            concept_pages.add(r.get("title", ""))

    return {
        "query": query,
        "complexity": complexity,
        "coverage": coverage,
        "coverage_level": coverage_level,
        "gap_topics": gap_topics,
        "results": results,
        "stats": {
            "total_hits": len(results),
            "high_confidence": high_conf,
            "directories": sorted(dirs),
            "concept_pages": sorted(concept_pages),
        },
    }


# ── Classify-Only Mode ───────────────────────────────────────────────

def classify_only(query: str) -> dict:
    """Lightweight mode: classify without full retrieval.

    Uses a minimal search (top 3) just enough to determine complexity.
    """
    keywords = _extract_query_keywords(query)
    results = search(query, top_k=3, backend="zvec")
    complexity = classify_complexity(query, results)
    coverage, coverage_level = compute_coverage(query, results, keywords)
    gap_topics = detect_gaps(keywords, results)

    return {
        "query": query,
        "complexity": complexity,
        "coverage": coverage,
        "coverage_level": coverage_level,
        "gap_topics": gap_topics,
        "keywords": keywords,
    }


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LLM Wiki Ask Engine: retrieve + classify + coverage + gaps")
    parser.add_argument("query", help="User's question")
    parser.add_argument("--top", type=int, default=None,
                        help="Override result count (default: auto by complexity)")
    parser.add_argument("--backend", choices=["zvec", "bigram"], default="zvec",
                        help="Search backend")
    parser.add_argument("--exclude-type", action="append", default=[],
                        help="Page type(s) to exclude")
    parser.add_argument("--classify-only", action="store_true",
                        help="Only classify complexity, skip full retrieval")
    args = parser.parse_args()

    exclude_types = set(args.exclude_type) if args.exclude_type else None

    if args.classify_only:
        output = classify_only(args.query)
    else:
        output = ask(
            args.query,
            top_k=args.top,
            backend=args.backend,
            exclude_types=exclude_types,
        )

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # Windows 管道/控制台默认 GBK：emoji 会让 print 崩掉（工作其实已完成）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
