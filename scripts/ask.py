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
import math
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

# ── 语料统计（IDF）：判断"你问的词，库里到底有没有" ────────────────
#
# 为什么需要 IDF：旧公式用"关键词是否字面出现在摘要里"算覆盖率，
# 于是"雅思口语怎么准备"这种问题——库里根本没有雅思内容，但"准备""考试"
# 这类高频词到处都是——会被判成 coverage=0.80"够了"，Agent 便拿不相干的
# 页面硬凑答案。用户问的往往是**稀有词**（雅思 / 红烧肉 / Kubernetes），
# 稀有词在结果标题里有没有被点到名，才是"库里有没有这块"的真信号。
#
# 实测（20 条真实提问 + 25 条库里明确没有的问题）：
#   - 旧公式：25 条负例 100% 被判成"有相关内容"（其中 1 条判成"够了"）
#   - 新公式：25 条负例 24 条 insufficient / 1 条 partial，正例 19/20 仍有料

_CORPUS_CACHE: dict = {"key": None, "texts": []}

# 虚词不进证据计算：小语料里它们的 df 也很低（6 页库里"是不是"只出现在 1 页），
# IDF 会被算得很高，反倒把"讲给别人听"这种有料的问题压成 insufficient。
_STOP_KW = {
    "怎么", "怎样", "如何", "什么", "为什么", "哪些", "哪个", "多少",
    "是不是", "有没有", "能不能", "要不要", "可不可以", "会不会", "对不对",
    "真的", "到底", "究竟", "应该", "需要", "可以", "能够", "可能",
    "帮我", "请问", "一下", "这个", "那个", "这些", "那些", "这样", "那样",
    "我们", "你们", "他们", "我的", "你的", "他的", "一个", "一些", "一种",
    "现在", "就是", "还是", "或者", "因为", "所以", "如果", "但是", "然后",
    "已经", "一直", "总是", "其实", "比较", "非常", "特别", "有点",
}

_MIN_TITLE_REL = 0.15   # 标题点名：命中标题的词须比"最大可能稀有度"高 15%（随语料规模自适应）
_MIN_BODY_COV = 0.40    # 正文覆盖兜底下限（标题没点名时）
_MAX_CORE_IDF = 4.0     # 核心词在库里至少出现 ~4 次；几乎不存在的词 → 一律判没料
_MAX_SCORE_WITH_GAP = 0.65  # 关键缺口词没覆盖时，分数封顶（只能 partial）
_MIN_BODY_REL = 0.40    # 正文兜底：命中正文的最稀有词 ≥ 核心词 IDF 的 40% 才算数
_GAP_MIN_IDF = 3.0      # 只把"够稀有且没出现"的词报成缺口（排除"怎么""准备"）


def _content_keywords(keywords: list[str]) -> list[str]:
    """去掉虚词后的实义关键词。"""
    return [k for k in keywords if k and k not in _STOP_KW]


def _norm(text: str) -> str:
    """匹配用的归一化：小写 + 去掉所有空白。

    "AI 味" 与标题里的 "AI味" 是同一个概念，但带空格的关键词永远匹配不上。
    """
    return re.sub(r"\s+", "", (text or "").lower())


def _merge_keywords(query: str, texts: list[str]) -> list[str]:
    """把相邻词拼回去，救回被 jieba 拆散的说法。

    例："AI 味" → jieba 切成 "AI" + "味"（"味" 只有 1 字，会被关键词过滤器丢掉），
    拼回 "ai味" 正好命中标题「…去AI味Skill…」；不拼就永远搜不到。
    这里用**不做长度过滤**的分词结果来拼，否则单字永远拼不回来。
    只保留库里真实存在的拼法（df>0），避免往证据里塞不存在的词。
    """
    try:
        import jieba
        toks = [t for t in jieba.cut(query) if t.strip() and re.search(r"\w", t)]
    except ImportError:
        toks = [t for t in query.split() if t.strip()]
    out: list[str] = []
    for a, b in zip(toks, toks[1:]):
        cand = _norm(a + b)
        if 3 <= len(cand) <= 8 and _df(cand, texts) > 0:
            out.append(cand)
    return list(dict.fromkeys(out))


def _corpus_texts() -> list[str]:
    """整库正文（小写）。按"页数 + 最新 mtime"缓存：MCP 长驻进程只扫一次库。"""
    try:
        pages = all_wiki_pages()
    except Exception:
        return []
    if not pages:
        return []
    try:
        key = (len(pages), round(max(p.stat().st_mtime for p in pages), 3))
    except Exception:
        key = (len(pages), 0)
    if _CORPUS_CACHE["key"] == key:
        return _CORPUS_CACHE["texts"]
    texts: list[str] = []
    for p in pages:
        try:
            texts.append(read_file(p).lower())
        except Exception:
            continue
    _CORPUS_CACHE.update({"key": key, "texts": texts})
    return texts


def _df(term: str, texts: list[str]) -> int:
    """该词出现在多少页里（document frequency）。"""
    t = (term or "").lower()
    if not t:
        return len(texts)
    return sum(1 for body in texts if t in body)


def _idf(term: str, texts: list[str]) -> float:
    """越高 = 越稀有 = 越能定义"用户到底在问什么"。"""
    n = max(1, len(texts))
    return math.log((n + 1) / (_df(term, texts) + 1))


def _kw_weights(keywords: list[str], texts: list[str]) -> dict[str, float]:
    return {k: _idf(k, texts) for k in keywords if k}


def compute_coverage(
    query: str,
    results: list[dict],
    keywords: list[str],
) -> tuple[float, str, str]:
    """算"这个库够不够回答"。

    Returns: (score, level, basis)
      - score: 0.0 - 1.0
      - level: "complete" | "partial" | "insufficient"
      - basis: "title_hit" | "body_hit" | "expanded_only" | "no_evidence"

    判据顺序（从强到弱）：
      1. 结果里必须有**直接命中**；只有双链邻居/实体搭桥 → 不算有料
      2. 标题点名率（IDF 加权）：你问的稀有词出现在某条结果标题里 → 库里有专页
      3. 正文兜底：标题没点名，但正文覆盖够、且定义性词在库里是常见词
         （说明是"换个说法而已"，不是"库里没有"）
      4. 其余一律判没料
    """
    if not results:
        return (0.0, "insufficient", "no_evidence")

    direct = [
        r for r in results
        if r.get("backend") not in ("keyword_fallback", "entity_bridge")
        and not r.get("expanded_from")
    ]
    if not direct:
        return (0.05, "insufficient", "expanded_only")

    kws = [k for k in keywords if k]
    if not kws:
        # 提不出关键词（极短问句）：只能看是否有直接命中
        return (0.45, "partial", "body_hit")

    texts = _corpus_texts()
    if not texts:
        # 拿不到语料（异常环境）：退回保守判断，绝不虚报"够了"
        return (0.35, "insufficient", "no_evidence")

    weights = _kw_weights(kws, texts)
    total = sum(weights.values()) or 1.0

    # 救回被分词拆散的说法（"AI 味" → "ai味"）。
    # 只用于**证据探测**，不进覆盖率分母——否则没命中的合并词会把你的真证据稀释掉。
    extras = {
        m: _idf(m, texts)
        for m in _merge_keywords(query, texts)
        if m not in weights
    }
    probe = {**weights, **extras}

    titles = _norm(" ".join((r.get("title") or "") for r in direct))
    body = titles + _norm(" ".join((r.get("snippet") or "") for r in direct))

    title_cov = sum(w for k, w in weights.items() if _norm(k) in titles) / total
    body_cov = sum(w for k, w in weights.items() if _norm(k) in body) / total

    # 核心词稀有度只看你自己问出来的实义词，不能用拼出来的词——
    # "内容做成"这种拼接碰巧在库里存在且极稀有，会把"做成产品"这种有料问题判没料。
    core_idf = max(weights.values()) if weights else 0.0
    max_title_idf = max(                   # 上了标题的最稀有词
        (w for k, w in probe.items() if _norm(k) in titles), default=0.0
    )
    best_body_idf = max(                   # 命中正文的最稀有词
        (w for k, w in probe.items() if _norm(k) in body), default=0.0
    )

    # 你问的那个关键点，结果里到底有没有提到？没有就是"细节缺口"。
    # 例：问"视频号小店保证金多少"，库里有视频号/小店话题（标题能对上），
    # 但没有任何一页提到"保证金" → 只能判 partial，绝不能判 complete。
    key_gaps = [
        k for k, w in weights.items()
        if w >= _GAP_MIN_IDF and _norm(k) not in body
    ]

    # 阈值随语料规模自适应：272 页库里 idf 上限 ≈5.6，6 页 demo 里只有 ≈1.95，
    # 写死绝对值会让小库永远凑不够"稀有"（demo 里连页面标题都判不成点名）。
    min_title_idf = _MIN_TITLE_REL * math.log(len(texts) + 1)
    if max_title_idf >= min_title_idf:
        # 标题点名了：越密集越接近 1.0（0.02 → 0.61，1.0 → 1.0）
        score = 0.60 + 0.40 * min(1.0, title_cov * 1.4)
        basis = "title_hit"
        if key_gaps:
            # 有专页但没答到你问的那一点 → 封顶 partial
            score = min(score, _MAX_SCORE_WITH_GAP)
    elif (body_cov >= _MIN_BODY_COV
          and core_idf <= _MAX_CORE_IDF
          and best_body_idf >= _MIN_BODY_REL * core_idf):
        # 标题换了说法，但命中正文的那个词本身够分量（≥ 核心词 IDF 的 40%）
        # → 中分：让调用方说"没有专页，相近的是…"
        # 相对判据挡的是"手机屏幕碎了换屏多少钱"这类：只靠"手机""屏幕"这种
        # 高频词蹭到正文（best_body_idf / core_idf ≈ 0.2），不能算有料。
        score = 0.45
        basis = "body_hit"
    else:
        score = min(0.38, round(body_cov * 0.45, 3))
        basis = "no_evidence"

    score = round(min(1.0, score), 3)
    if score >= 0.7:
        level = "complete"
    elif score >= 0.4:
        level = "partial"
    else:
        level = "insufficient"
    return (score, level, basis)


# ── Gap Detection ────────────────────────────────────────────────────

def detect_gaps(
    keywords: list[str],
    results: list[dict],
) -> list[str]:
    """报出"够稀有、且结果标题里完全没出现"的查询词。

    旧版把任何没出现在摘要里的词都算缺口，于是"雅思口语怎么准备"报的是
    ['准备']——恰好把唯一有信息量的词漏掉了。现在按 IDF 过滤，只报
    "库里确实没有"的那些词（雅思 / 红烧肉 / Kubernetes）。
    """
    if not keywords:
        return []
    if not results:
        return list(keywords)

    texts = _corpus_texts()
    weights = _kw_weights(keywords, texts) if texts else {}
    titles = _norm(" ".join((r.get("title") or "") for r in results))
    body = titles + _norm(" ".join((r.get("snippet") or "") for r in results))

    gaps = []
    for kw in keywords:
        if not kw:
            continue
        w = weights.get(kw, 0.0)
        if w < _GAP_MIN_IDF:
            continue          # 常见词缺失不算缺口（"怎么""准备""方法"）
        if _norm(kw) not in body:
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
    coverage, coverage_level, coverage_basis = compute_coverage(query, results, keywords)

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
        "coverage_basis": coverage_basis,
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
    coverage, coverage_level, coverage_basis = compute_coverage(query, results, keywords)
    gap_topics = detect_gaps(keywords, results)

    return {
        "query": query,
        "complexity": complexity,
        "coverage": coverage,
        "coverage_level": coverage_level,
        "coverage_basis": coverage_basis,
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
