#!/usr/bin/env python3
"""
Hybrid Search for LLM Wiki: Zvec (FTS + Vector + RRF) with CJK bigram fallback.

Three-stage pipeline (Zvec mode):
  1. Zvec MultiQuery: FTS (jieba+BM25) + Vector (HNSW) + RRF fusion
  2. Wikilink neighbor expansion (preserved from v1)
  3. Output JSON for Hermes AI semantic re-ranking (optional)

Fallback pipeline (bigram mode):
  1. CJK bigram keyword matching → Top-10 candidates
  2. Graph neighbor expansion → Top-15
  3. Output JSON

Usage:
    python scripts/search.py "信息成瘾"
    python scripts/search.py "注意力经济" --top 5
    python scripts/search.py "value creator" --top 10
    python scripts/search.py "知识" --backend bigram    # Force bigram mode

Output: JSON array of candidates, each with:
  {path, title, score, snippet, type, neighbors, page_id}
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import sys
from pathlib import Path

# Bootstrap shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from _utils import (
    DATA_ROOT, WIKI_DIR, RAW_DIR, GITHUB_STARS_DIR,
    read_file, strip_frontmatter, extract_frontmatter_field,
    extract_wikilinks, all_wiki_pages, page_id, page_name,
    has_cjk, cjk_bigrams, get_neighbors,
    sha256, ensure_data_dir, SEARCH_CACHE,
)
from zvec_config import (
    ZVEC_DB_DIR, EMBEDDING_MODEL, EMBEDDING_DIMENSION,
    FIELD_TITLE, FIELD_CONTENT, FIELD_CATEGORY, FIELD_TYPE,
    FIELD_SOURCE_PATH, FIELD_STATUS, VECTOR_FIELD,
    encode_doc_id, get_or_create_collection, load_embedding_model,
    DEFAULT_TOPK, RRF_RANK_CONSTANT,
)

# ── Scoring Constants (bigram fallback) ────────────────────────────────

WEIGHT_FILENAME_EXACT = 200
WEIGHT_FILENAME_CONTAINS = 100
WEIGHT_TITLE_EXACT = 50
WEIGHT_TITLE_CONTAINS = 25
WEIGHT_BODY_PER_MATCH = 20
WEIGHT_NEIGHBOR = 10

SOURCE_TIER = {
    "concept": 1.3,
    "entity": 1.2,
    "source": 1.0,
    "synthesis": 1.4,
    "snippet": 1.1,
    "topic": 1.2,
    "distill": 1.15,
    "github-star": 0.8,
    "github-star-archived": 0.5,
}

SNIPPET_MAX_LEN = 200

# ── Evidence Tiering (2026-10-26 修复排序 bug) ────────────────────────
# 历史 bug：双链邻居一律 score = seed*0.3 + WEIGHT_NEIGHBOR(10)，而语义直接命中
# 的 RRF 分只有 ~0.03。全部结果按 score 全局排序 → 扩展结果永远压在直接命中之上，
# 正确文档被挤到第 4-5 位甚至截断丢弃（实测 18 条评测 top-1 命中 0/18）。
# 修复：不跨来源比较分数，按“证据层级”排序 —— 直接命中 > 关键词兜底 > 关联扩展。
TIER_DIRECT = 0      # Zvec FTS+向量 RRF 直接命中
TIER_KEYWORD = 1     # 关键词兜底（文件名/标题精确匹配）
TIER_EXPANDED = 2    # 双链邻居 / 实体搭桥扩展（补充证据，不占主结果位）


def rank_tier(item: dict) -> int:
    """判定结果的证据层级（0 最好）。"""
    if item.get("expanded_from"):
        return TIER_EXPANDED
    if item.get("backend") == "keyword_fallback":
        return TIER_KEYWORD
    return TIER_DIRECT


def rank_key(item: dict) -> tuple[int, float]:
    """稳定排序键：先按证据层级，再按同层级内原始分数降序。"""
    return (rank_tier(item), -float(item.get("score") or 0.0))


# ── P0: Soft Segmentation (Score Cliff Detection) ─────────────────────
# When adjacent search results show a sharp score drop, the results before
# the cliff are "high confidence" and after are "low confidence".
# This prevents low-quality results from being treated as equal evidence.

CLIFF_DROP_RATIO = 0.3      # Fraction of max_score that counts as a cliff
CLIFF_MIN_RESULTS = 2       # Minimum high-confidence results before allowing a cliff

# ── P1: Keyword Fallback ──────────────────────────────────────────────
# When RRF fusion suppresses a document that exactly matches a query keyword,
# this fallback re-surfaces it via pure FTS matching.

KEYWORD_FALLBACK_TRIGGER = 0.3   # Trigger if Top1 RRF score < this threshold
KEYWORD_FALLBACK_TOP_K = 2       # Max supplementary results to add
KEYWORD_MIN_LEN = 2              # Minimum keyword length after jieba cut

# ── P3: Diversity Control ─────────────────────────────────────────────
# Prevents search results from being dominated by a single directory/topic.

DIVERSITY_MAX_SAME_DIR = 2       # Max results from the same directory
DIVERSITY_PENALTY = 0.5          # Score multiplier for excess same-dir results


# ── Zvec Hybrid Search ────────────────────────────────────────────────

def zvec_search(query: str, top_k: int = 5, exclude_types: set[str] | None = None) -> list[dict]:
    """Zvec MultiQuery: FTS (jieba+BM25) + Vector (HNSW) + RRF fusion.

    Args:
        query: Search query string
        top_k: Number of results to return
        exclude_types: Set of page types to exclude (e.g. {"github-star"})
    """
    import zvec
    from zvec.model.param.query import Fts, Query
    from zvec.extension.multi_vector_reranker import RrfReRanker

    # Step 1: Generate query embedding (temporary model load)
    model = load_embedding_model()
    q_embeddings = list(model.embed([query]))
    q_vec = q_embeddings[0].tolist() if hasattr(q_embeddings[0], 'tolist') else list(q_embeddings[0])
    del model
    gc.collect()

    # Step 2: Zvec MultiQuery
    coll = get_or_create_collection()
    reranker = RrfReRanker(rank_constant=RRF_RANK_CONSTANT)

    try:
        results = coll.query(
            queries=[
                Query(field_name=FIELD_CONTENT, fts=Fts(match_string=query)),
                Query(field_name=VECTOR_FIELD, vector=q_vec),
            ],
            topk=top_k * 2,  # Fetch more for neighbor expansion
            reranker=reranker,
        )
    except Exception as e:
        print(f"[search] Zvec MultiQuery failed: {e}", file=sys.stderr)
        raise

    # Step 3: Map Zvec results to wiki file paths
    exclude = exclude_types or set()
    mapped = []
    for r in results:
        r_type = r.fields.get(FIELD_TYPE, "source")
        if r_type in exclude:
            continue

        source_path = r.fields.get(FIELD_SOURCE_PATH, "")
        full_path = DATA_ROOT / source_path
        if not full_path.exists():
            continue

        content = read_file(full_path)
        body = strip_frontmatter(content)
        snippet = body[:SNIPPET_MAX_LEN].replace("\n", " ").strip()
        if len(body) > SNIPPET_MAX_LEN:
            snippet += "..."

        item = {
            "path": source_path,
            "title": r.fields.get(FIELD_TITLE, ""),
            "score": round(r.score, 4),
            "snippet": snippet,
            "type": r.fields.get(FIELD_TYPE, "source"),
            "page_id": page_id(full_path),
            "neighbors": [],
            "backend": "zvec",
        }
        mapped.append(item)

    # Step 4: Wikilink neighbor expansion
    if mapped:
        mapped = _expand_with_neighbors(mapped, top_k=top_k + 5, exclude_types=exclude_types)

    # Step 5: Entity bridge expansion
    if mapped:
        mapped = _expand_with_entity_bridge(mapped, top_k=top_k + 5, exclude_types=exclude_types)

    # Step 6 (P1): Keyword fallback — supplement if direct hits are weak
    # Check direct hit scores (not expanded results, which have inflated scores)
    direct_hit_scores = [r["score"] for r in mapped if not r.get("expanded_from")]
    if direct_hit_scores and max(direct_hit_scores) < KEYWORD_FALLBACK_TRIGGER:
        supplements = _keyword_fallback(query, mapped, top_k=KEYWORD_FALLBACK_TOP_K, exclude_types=exclude_types)
        if supplements:
            mapped.extend(supplements)
            mapped.sort(key=rank_key)

    # Step 7 (P3): Diversity control — penalize over-represented directories
    if len(mapped) > 1:
        mapped = _diversify_results(mapped)

    # Step 8 (2026-10-26 简化): 分层排序后直接命中原生就在列表头部，
    # 不再需要“从尾部替换扩展结果”的补丁逻辑（原逻辑会把高相关直接命中
    # 塞到第 4-5 位，是排序 bug 的另一半）。
    result = mapped[:top_k]

    # Step 9 (P0): Soft segmentation — label confidence levels
    # Must run AFTER Step 8 so that all final results get labeled
    result = _apply_confidence_labels(result)

    return result


# ── P0: Soft Segmentation ────────────────────────────────────────────

def detect_score_cliff(results: list[dict], drop_ratio: float = CLIFF_DROP_RATIO) -> int | None:
    """Detect a score cliff in ranked search results.

    A "cliff" is a point where the score drops sharply relative to the
    maximum score, indicating a boundary between high-confidence and
    low-confidence results.

    Args:
        results: Search results sorted by score (descending). Each must have a "score" key.
        drop_ratio: Fraction of max_score that constitutes a cliff (default 0.3).

    Returns:
        Index of the first low-confidence result (i.e. cliff position),
        or None if no cliff detected. Results[:cliff] are high-confidence,
        results[cliff:] are low-confidence.

    Example:
        scores = [0.95, 0.92, 0.88, 0.45, 0.42]
        cliff = 3  → results[0:3] are high-confidence, results[3:] are low
    """
    if len(results) < 3:
        return None

    max_score = results[0]["score"]
    if max_score <= 0:
        return None

    for i in range(1, len(results)):
        # Only detect cliff after at least CLIFF_MIN_RESULTS high-confidence hits
        if i < CLIFF_MIN_RESULTS:
            continue
        drop = (results[i - 1]["score"] - results[i]["score"]) / max_score
        if drop > drop_ratio:
            return i

    return None


def _apply_confidence_labels(results: list[dict]) -> list[dict]:
    """Apply confidence labels to search results based on score cliff detection.

    Adds a "confidence" field to each result:
      - "high": Above the score cliff (strong match)
      - "low": Below the score cliff (weak match)

    If no cliff is detected, all results get "high" confidence.
    Results expanded via neighbors or entity bridge always get "low" confidence
    (they are supplementary, not direct search hits).

    Note: Direct hits and expanded results may have different score scales
    (e.g. Zvec RRF scores ~0.01-0.02, neighbor expansion scores ~10+).
    We detect cliffs separately within each group to avoid cross-scale artifacts.
    """
    if not results:
        return results

    # 按证据层级分组打分（2026-10-26）：不同来源分数尺度不同（RRF ~0.03，
    # 关键词兜底 1-200，扩展 10+），必须各自判悬崖，不能用一条曲线混算。
    for tier in (TIER_DIRECT, TIER_KEYWORD):
        group = [r for r in results if rank_tier(r) == tier]
        cliff_idx = detect_score_cliff(group)
        for i, r in enumerate(group):
            r["confidence"] = "low" if (cliff_idx is not None and i >= cliff_idx) else "high"

    expanded = [r for r in results if rank_tier(r) == TIER_EXPANDED]
    exp_cliff_idx = detect_score_cliff(expanded)
    for i, r in enumerate(expanded):
        if exp_cliff_idx is not None and i >= exp_cliff_idx:
            r["confidence"] = "low"
        else:
            # 扩展结果是补充证据，不占主结果位，最高只能给 medium
            r["confidence"] = "medium"

    return results


# ── P1: Keyword Fallback ──────────────────────────────────────────────

def _keyword_fallback(query: str, existing_results: list[dict], top_k: int = KEYWORD_FALLBACK_TOP_K, exclude_types: set[str] | None = None) -> list[dict]:
    """Supplement RRF results with pure FTS keyword matches.

    When RRF fusion pushes down a document that exactly matches a query keyword
    (because its vector score was too low), this function re-surfaces it by
    running a pure FTS query for each keyword extracted from the query.

    Only triggered when the top RRF result's score is below KEYWORD_FALLBACK_TRIGGER,
    indicating the search may have missed relevant exact matches.

    Fallback strategy:
      1. Try Zvec FTS (fast, indexed)
      2. If Zvec FTS returns 0 results (known bug in some Zvec versions),
         fall back to jieba + file content scan (slower but reliable)

    Args:
        query: Original search query.
        existing_results: Results from RRF search (to avoid duplicates).
        top_k: Max supplementary results to add.

    Returns:
        List of supplementary result dicts (may be empty).
    """
    # Extract keywords via jieba
    try:
        import jieba
        keywords = [w for w in jieba.cut(query) if len(w) >= KEYWORD_MIN_LEN]
    except ImportError:
        keywords = [w for w in query.split() if len(w) >= KEYWORD_MIN_LEN]

    if not keywords:
        return []

    existing_ids = {r["page_id"] for r in existing_results if r.get("page_id")}
    existing_paths = {r["path"] for r in existing_results if r.get("path")}
    supplements = []
    seen_ids = set()

    # Strategy 1: Try Zvec FTS
    try:
        import zvec
        from zvec.model.param.query import Fts, Query

        coll = get_or_create_collection()
        fts_worked = False

        for kw in keywords:
            if len(supplements) >= top_k:
                break

            try:
                fts_results = coll.query(
                    queries=[Query(field_name=FIELD_CONTENT, fts=Fts(match_string=kw))],
                    topk=3,
                )
            except Exception as e:
                print(f"[search] Keyword fallback FTS failed for '{kw}': {e}", file=sys.stderr)
                continue

            if fts_results:
                fts_worked = True

            for r in fts_results:
                source_path = r.fields.get(FIELD_SOURCE_PATH, "")
                full_path = DATA_ROOT / source_path
                if not full_path.exists():
                    continue

                real_pid = page_id(full_path)
                if real_pid in existing_ids or real_pid in seen_ids or source_path in existing_paths:
                    continue

                # Exclude types (e.g. github-star) — same filter as Strategy 2 and main Zvec path
                r_type = r.fields.get(FIELD_TYPE, "source")
                if exclude_types and r_type in (exclude_types or set()):
                    continue

                content = read_file(full_path)
                body = strip_frontmatter(content)
                snippet = body[:SNIPPET_MAX_LEN].replace("\n", " ").strip()
                if len(body) > SNIPPET_MAX_LEN:
                    snippet += "..."

                supplements.append({
                    "path": source_path,
                    "title": r.fields.get(FIELD_TITLE, ""),
                    "score": round(r.score, 4),
                    "snippet": snippet,
                    "type": r.fields.get(FIELD_TYPE, "source"),
                    "page_id": real_pid,
                    "neighbors": [],
                    "backend": "keyword_fallback",
                    "confidence": "low",
                    "fallback_keyword": kw,
                })
                seen_ids.add(real_pid)

        # If Zvec FTS returned any results, we're done
        if fts_worked:
            return supplements

    except Exception as e:
        print(f"[search] Zvec FTS unavailable for keyword fallback: {e}", file=sys.stderr)

    # Strategy 2: File content scan fallback (when Zvec FTS is broken)
    # Scan all wiki pages for exact keyword matches
    if len(supplements) >= top_k:
        return supplements

    pages = all_wiki_pages()
    # Score pages by keyword match count
    kw_scores: dict[str, tuple[float, Path]] = {}  # page_id → (score, path)
    for p in pages:
        pid = page_id(p)
        if pid in existing_ids or pid in seen_ids:
            continue

        content = read_file(p)
        ptype = extract_frontmatter_field(content, "type") or "source"
        if exclude_types and ptype in (exclude_types or set()):
            continue

        body = strip_frontmatter(content).lower()
        title = (extract_frontmatter_field(content, "title") or page_name(p)).lower()

        match_score = 0.0
        for kw in keywords:
            kw_lower = kw.lower()
            # Title exact match = highest signal
            if title == kw_lower:
                match_score += 10.0
            elif kw_lower in title:
                match_score += 5.0
            # Body match
            count = body.count(kw_lower)
            if count > 0:
                match_score += min(count, 5) * 1.0  # Cap at 5 occurrences

        if match_score > 0:
            kw_scores[pid] = (match_score, p)

    # Sort by match score, take top_k
    sorted_matches = sorted(kw_scores.items(), key=lambda x: x[1][0], reverse=True)
    for pid, (score, p) in sorted_matches:
        if len(supplements) >= top_k:
            break

        source_path = str(p.relative_to(DATA_ROOT)).replace("\\", "/")
        content = read_file(p)
        body = strip_frontmatter(content)
        snippet = body[:SNIPPET_MAX_LEN].replace("\n", " ").strip()
        if len(body) > SNIPPET_MAX_LEN:
            snippet += "..."

        supplements.append({
            "path": source_path,
            "title": extract_frontmatter_field(content, "title") or page_name(p),
            "score": round(score, 4),
            "snippet": snippet,
            "type": extract_frontmatter_field(content, "type") or "source",
            "page_id": pid,
            "neighbors": [],
            "backend": "keyword_fallback",
            "confidence": "low",
            "fallback_keyword": ",".join(keywords[:3]),
        })
        seen_ids.add(pid)

    return supplements


# ── P3: Diversity Control ──────────────────────────────────────────────

def _diversify_results(results: list[dict], max_same_dir: int = DIVERSITY_MAX_SAME_DIR, penalty: float = DIVERSITY_PENALTY) -> list[dict]:
    """Penalize results that over-represent a single directory/topic.

    For results sharing the same directory prefix (e.g. "summaries/AI & 技术/"),
    only the top `max_same_dir` keep their original score. The rest get
    their score multiplied by `penalty`, pushing them down in ranking.

    Args:
        results: Search results sorted by score (descending).
        max_same_dir: Max results allowed from the same directory at full score.
        penalty: Score multiplier for excess same-directory results (0-1).

    Returns:
        Results re-sorted after penalty application.
    """
    if not results:
        return results

    dir_counts: dict[str, int] = {}

    for r in results:
        path = r.get("path", "")
        # Extract directory: "summaries/AI & 技术/xxx.md" → "summaries/AI & 技术"
        parts = path.rsplit("/", 1)
        dir_key = parts[0] if len(parts) > 1 else "_root"

        dir_counts.setdefault(dir_key, 0)
        dir_counts[dir_key] += 1

        if dir_counts[dir_key] > max_same_dir:
            r["score"] = round(r["score"] * penalty, 4)
            r["diversity_penalized"] = True

    # Re-sort after penalty (分层排序，避免多样性惩罚打乱证据层级)
    results.sort(key=rank_key)
    return results


# ── CJK Bigram Fallback Search ────────────────────────────────────────

def tokenize_query(query: str) -> tuple[list[str], list[str]]:
    """Tokenize query into CJK bigrams and English words."""
    cjk_tokens = []
    eng_tokens = []

    cjk_chars = ""
    for ch in query:
        if '\u4e00' <= ch <= '\u9fff':
            cjk_chars += ch
        else:
            if len(cjk_chars) >= 2:
                cjk_tokens.extend(cjk_bigrams(cjk_chars))
            cjk_chars = ""
    if len(cjk_chars) >= 2:
        cjk_tokens.extend(cjk_bigrams(cjk_chars))

    eng_tokens = [w.lower() for w in re.findall(r'[a-zA-Z]{3,}', query)]
    return cjk_tokens, eng_tokens


def score_page(page_path: Path, cjk_tokens: list[str], eng_tokens: list[str], query: str) -> float:
    """Score a single page against the query tokens (bigram mode)."""
    score = 0.0
    stem = page_path.stem.lower()
    title = (extract_frontmatter_field(read_file(page_path), "title") or "").lower()
    body = strip_frontmatter(read_file(page_path)).lower()

    if stem == query.lower():
        score += WEIGHT_FILENAME_EXACT
    elif query.lower() in stem:
        score += WEIGHT_FILENAME_CONTAINS

    if title and title == query.lower():
        score += WEIGHT_TITLE_EXACT
    elif title and query.lower() in title:
        score += WEIGHT_TITLE_CONTAINS

    for token in cjk_tokens:
        count = body.count(token)
        score += count * WEIGHT_BODY_PER_MATCH
        if token in title:
            score += WEIGHT_TITLE_CONTAINS

    for token in eng_tokens:
        count = body.count(token)
        score += count * WEIGHT_BODY_PER_MATCH
        if token in title:
            score += WEIGHT_TITLE_CONTAINS

    page_type = extract_frontmatter_field(read_file(page_path), "type") or "source"
    tier = SOURCE_TIER.get(page_type, 1.0)
    score *= tier

    return score


def bigram_search(query: str, top_k: int = 10, exclude_types: set[str] | None = None) -> list[dict]:
    """CJK bigram keyword search (fallback mode).

    Args:
        query: Search query string
        top_k: Number of results to return
        exclude_types: Set of page types to exclude (e.g. {"github-star"})
    """
    cjk_tokens, eng_tokens = tokenize_query(query)
    if not cjk_tokens and not eng_tokens:
        eng_tokens = [query.lower()]

    exclude = exclude_types or set()
    pages = all_wiki_pages()
    results = []

    for p in pages:
        content = read_file(p)
        page_type = extract_frontmatter_field(content, "type") or "source"
        if page_type in exclude:
            continue

        s = score_page(p, cjk_tokens, eng_tokens, query)
        if s > 0:
            body = strip_frontmatter(content)
            snippet = body[:SNIPPET_MAX_LEN].replace("\n", " ").strip()
            if len(body) > SNIPPET_MAX_LEN:
                snippet += "..."

            results.append({
                "path": str(p.relative_to(DATA_ROOT)).replace("\\", "/"),
                "title": extract_frontmatter_field(content, "title") or page_name(p),
                "score": round(s, 2),
                "snippet": snippet,
                "type": extract_frontmatter_field(content, "type") or "source",
                "page_id": page_id(p),
                "neighbors": [],
                "backend": "bigram",
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


# ── Wikilink Neighbor Expansion ────────────────────────────────────────

def _build_wikilink_edges() -> list[dict]:
    """Build edges from [[wikilinks]] in real-time."""
    pages = all_wiki_pages()
    page_map = {}
    for p in pages:
        content = read_file(p)
        pid = page_id(p)
        ptype = extract_frontmatter_field(content, "type") or "source"
        page_map[pid] = ptype
        page_map[page_name(p)] = ptype

    edges = []
    for p in pages:
        content = read_file(p)
        pid = page_id(p)
        links = extract_wikilinks(content)
        for link in links:
            target_id = None
            for existing_id in page_map:
                if existing_id == link or existing_id.endswith(f"/{link}"):
                    target_id = existing_id
                    break
            if target_id and target_id != pid:
                edges.append({
                    "from": pid,
                    "to": target_id,
                    "type": "EXTRACTED",
                    "source": "auto-link",
                })
    return edges


def _expand_with_neighbors(candidates: list[dict], top_k: int = 15, exclude_types: set[str] | None = None) -> list[dict]:
    """Expand candidates using wikilink neighbor edges.

    Args:
        candidates: Initial search results
        top_k: Maximum results to return
        exclude_types: Set of page types to exclude from neighbor expansion
    """
    exclude = exclude_types or set()
    edges = _build_wikilink_edges()
    if not edges:
        return candidates[:top_k]

    existing_ids = {c["page_id"] for c in candidates}
    neighbor_additions = []

    for c in candidates:
        neighbors = get_neighbors(c["page_id"], edges, min_confidence=0.7)
        c["neighbors"] = sorted(neighbors)

        for nid in neighbors:
            if nid in existing_ids:
                continue
            # 依次尝试 wiki/ 与 github-stars/ 定位邻居路径
            neighbor_path = WIKI_DIR / f"{nid}.md"
            if not neighbor_path.exists():
                neighbor_path = GITHUB_STARS_DIR / f"{nid}.md"
            if not neighbor_path.exists():
                for p in all_wiki_pages():
                    if page_id(p) == nid:
                        neighbor_path = p
                        break
                else:
                    continue

            content = read_file(neighbor_path)
            n_type = extract_frontmatter_field(content, "type") or "source"
            if n_type in exclude:
                continue
            body = strip_frontmatter(content)
            snippet = body[:SNIPPET_MAX_LEN].replace("\n", " ").strip()
            if len(body) > SNIPPET_MAX_LEN:
                snippet += "..."

            neighbor_additions.append({
                "path": str(neighbor_path.relative_to(DATA_ROOT)).replace("\\", "/"),
                "title": extract_frontmatter_field(content, "title") or page_name(neighbor_path),
                "score": c["score"] * 0.3 + WEIGHT_NEIGHBOR,
                "snippet": snippet,
                "type": extract_frontmatter_field(content, "type") or "source",
                "page_id": nid,
                "neighbors": [],
                "expanded_from": c["page_id"],
                "backend": c.get("backend", "unknown"),
            })
            existing_ids.add(nid)

    all_results = candidates + neighbor_additions
    all_results.sort(key=rank_key)  # 分层排序：直接命中 > 扩展（2026-10-26 修复）
    return all_results[:top_k]


# ── Entity Bridge Expansion ───────────────────────────────────────────

def _expand_with_entity_bridge(candidates: list[dict], top_k: int = 15, exclude_types: set[str] | None = None) -> list[dict]:
    """Expand candidates using entity bridge — find chunks sharing entities with search hits.

    Three-layer filter (see entity_index.py bridge_by_entities for details):
      1. Shared ≥ 2 entities → bridge (strong)
      2. Shared 1 entity, both core → bridge (medium)
      3. Shared 1 entity, at least one mention → skip (noise)

    Graceful degradation: if entity_index.db doesn't exist or bridge fails,
    silently returns candidates unchanged.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from entity_index import bridge_by_entities, ENTITY_DB
    except ImportError:
        return candidates[:top_k]

    if not ENTITY_DB.exists():
        return candidates[:top_k]

    try:
        seed_page_ids = [c["page_id"] for c in candidates if c.get("page_id")]
        seed_scores = {c["page_id"]: c["score"] for c in candidates if c.get("page_id") and c.get("score")}

        if not seed_page_ids:
            return candidates[:top_k]

        bridge_results = bridge_by_entities(
            seed_page_ids=seed_page_ids,
            seed_scores=seed_scores,
            top_k=top_k,
            exclude_types=exclude_types,
        )
    except Exception as e:
        print(f"[search] Entity bridge failed: {e}", file=sys.stderr)
        return candidates[:top_k]

    if not bridge_results:
        return candidates[:top_k]

    # Merge bridge results into candidates
    existing_ids = {c["page_id"] for c in candidates}
    bridge_additions = []

    for r in bridge_results:
        if r["page_id"] in existing_ids:
            continue

        full_path = DATA_ROOT / r["source_path"]
        if not full_path.exists():
            continue

        content = read_file(full_path)
        body = strip_frontmatter(content)
        snippet = body[:SNIPPET_MAX_LEN].replace("\n", " ").strip()
        if len(body) > SNIPPET_MAX_LEN:
            snippet += "..."

        bridge_additions.append({
            "path": r["source_path"],
            "title": extract_frontmatter_field(content, "title") or page_name(full_path),
            "score": r["score"],
            "snippet": snippet,
            "type": extract_frontmatter_field(content, "type") or "source",
            "page_id": r["page_id"],
            "neighbors": [],
            "expanded_from": "entity_bridge",
            "shared_entities": r.get("shared_entities", []),
            "backend": "entity_bridge",
        })
        existing_ids.add(r["page_id"])

    all_results = candidates + bridge_additions
    all_results.sort(key=rank_key)  # 分层排序：直接命中 > 扩展（2026-10-26 修复）
    return all_results[:top_k]


# ── Similar Document Recommendation ───────────────────────────────────

def similar_docs(page_path: str, top_k: int = 5) -> list[dict]:
    """Find documents similar to a given wiki page using vector search.

    Uses the page's title + first paragraph as the query for vector search.
    Excludes the page itself from results.
    """
    p = Path(page_path)
    if not p.exists():
        # Try relative to WIKI_DIR
        p = WIKI_DIR / page_path
    if not p.exists():
        # Try relative to REPO_ROOT
        p = DATA_ROOT / page_path
    if not p.exists():
        print(f"[similar] Page not found: {page_path}", file=sys.stderr)
        return []

    content = read_file(p)
    body = strip_frontmatter(content)
    title = extract_frontmatter_field(content, "title") or page_name(p)

    # Resolve to absolute path for reliable relative_to
    p = p.resolve()
    self_source_path = str(p.relative_to(DATA_ROOT)).replace("\\", "/")

    # Use title + first 800 chars as the similarity query
    # (fastembed has ~512 token limit ≈ 800-1000 CJK chars, so 800 chars uses the full window)
    query_text = f"{title}。{body[:800]}"

    # P2: FTS + Vector hybrid search (was: vector-only)
    # Use the same dual-path RRF pipeline as zvec_search() for consistent quality.
    try:
        import zvec
        from zvec.model.param.query import Fts, Query
        from zvec.extension.multi_vector_reranker import RrfReRanker

        model = load_embedding_model()
        q_embeddings = list(model.embed([query_text]))
        q_vec = q_embeddings[0].tolist() if hasattr(q_embeddings[0], 'tolist') else list(q_embeddings[0])
        del model
        gc.collect()

        coll = get_or_create_collection()
        reranker = RrfReRanker(rank_constant=RRF_RANK_CONSTANT)

        results = coll.query(
            queries=[
                Query(field_name=FIELD_CONTENT, fts=Fts(match_string=query_text[:200])),  # FTS with title+snippet
                Query(field_name=VECTOR_FIELD, vector=q_vec),
            ],
            topk=top_k + 1,  # +1 to exclude self
            reranker=reranker,
        )

        mapped = []
        for r in results:
            source_path = r.fields.get(FIELD_SOURCE_PATH, "")
            # Skip self by comparing source_path
            if source_path == self_source_path:
                continue
            full_path = DATA_ROOT / source_path
            if not full_path.exists():
                continue

            rcontent = read_file(full_path)
            rbody = strip_frontmatter(rcontent)
            snippet = rbody[:SNIPPET_MAX_LEN].replace("\n", " ").strip()
            if len(rbody) > SNIPPET_MAX_LEN:
                snippet += "..."

            mapped.append({
                "path": source_path,
                "title": r.fields.get(FIELD_TITLE, ""),
                "score": round(r.score, 4),
                "snippet": snippet,
                "type": r.fields.get(FIELD_TYPE, ""),
                "page_id": page_id(full_path),
                "neighbors": [],
                "backend": "similar",
            })

        # Entity bridge expansion for similar docs
        if mapped:
            mapped = _expand_with_entity_bridge(mapped, top_k=top_k + 2)

        return mapped[:top_k]

    except Exception as e:
        print(f"[similar] Vector search failed: {e}, falling back to keyword search", file=sys.stderr)
        # Fallback: use title as keyword search
        return search(title, top_k=top_k, backend="bigram")


# ── Duplicate Detection ───────────────────────────────────────────────

def check_duplicate(page_path: str, threshold: float = 0.08) -> list[dict]:
    """Check if a wiki page is semantically duplicate of existing pages.

    Uses a pure vector query to get TRUE cosine distances. RRF fused scores
    (from similar_docs) are rank-based (range ~0.01-0.03) and can never be
    compared to a fixed threshold — that caused every page to be flagged.

    Zvec COSINE metric returns distance: distance = 1 - cosine_similarity
    for normalized vectors. So a distance of 0.08 means ~92% similar.
    Lower distance = more similar.
    """
    import zvec
    from zvec.model.param.query import Query

    p = Path(page_path)
    if not p.exists():
        p = WIKI_DIR / page_path
    if not p.exists():
        p = DATA_ROOT / page_path
    if not p.exists():
        return []

    content = read_file(p)
    body = strip_frontmatter(content)
    title = extract_frontmatter_field(content, "title") or page_name(p)
    p = p.resolve()
    self_source_path = str(p.relative_to(DATA_ROOT)).replace("\\", "/")

    # Query embedding (temporary model load, same as zvec_search)
    model = load_embedding_model()
    q_embeddings = list(model.embed([f"{title}。{body[:800]}"]))
    q_vec = q_embeddings[0].tolist() if hasattr(q_embeddings[0], 'tolist') else list(q_embeddings[0])
    del model
    gc.collect()

    coll = get_or_create_collection()
    results = coll.query(
        queries=[Query(field_name=VECTOR_FIELD, vector=q_vec)],
        topk=10,
    )

    # Filter by cosine distance (lower = more similar)
    duplicates = []
    for r in results:
        source_path = r.fields.get(FIELD_SOURCE_PATH, "")
        if not source_path or source_path == self_source_path:
            continue
        full_path = DATA_ROOT / source_path
        if not full_path.exists():
            continue
        if r.score <= threshold:
            duplicates.append({
                "path": source_path,
                "title": r.fields.get(FIELD_TITLE, ""),
                "score": round(r.score, 4),
                "type": r.fields.get(FIELD_TYPE, ""),
                "page_id": page_id(full_path),
                "backend": "zvec",
            })

    return duplicates


# ── Main Search Entry Point ───────────────────────────────────────────

STAR_TYPES = {"github-star", "github-star-archived"}


def _effective_type(path, declared: "str | None") -> str:
    """文件级扫描路径统一类型：raw/ 下的原文一律算 raw。

    否则它 frontmatter 里写的 type: note 会绕过 exclude_types={'raw'} 混进默认检索。
    """
    try:
        if RAW_DIR in Path(path).resolve().parents:
            return "raw"
    except Exception:
        pass
    return declared or "source"


def _enforce_type_filters(results: list[dict], exclude_types: set[str] | None) -> list[dict]:
    """最后一道闸：路径即真相。

    raw/ 与 github-stars/ 的身份只靠路径判定（raw 页 frontmatter 写的是 type: note；
    收藏仓库 page_id 不带 github-stars/ 前缀）。新增的结果构造路径自动受保护。
    """
    if not exclude_types:
        return results
    out = []
    for r in results:
        pid = str(r.get("page_id") or "")
        path = str(r.get("path") or "")
        if pid.startswith("raw/") or path.startswith("raw/"):
            r["type"] = "raw"
        elif path.startswith("github-stars/") or pid.startswith("github-stars/"):
            r["type"] = r.get("type") or "github-star"
        if r.get("type") in exclude_types:
            continue
        out.append(r)
    return out


def search(query: str, top_k: int = 5, backend: str = "zvec",
           exclude_types: set[str] | None = None, include_stars: bool = False,
           include_raw: bool = False) -> list[dict]:
    """Full search pipeline: Zvec hybrid → bigram fallback.

    Args:
        query: Search query string
        top_k: Number of results to return
        backend: "zvec" (default) or "bigram" (forced fallback)
        exclude_types: Set of page types to exclude (e.g. {"github-star"})
        include_stars: 默认 False。github-stars/ 是"我收藏的仓库"，与自有知识物理隔离，
            查收藏项目用 ghstars；wiki 检索默认不掺进来（否则"视频制作"5 条全是仓库）。
        include_raw: 默认 False。raw/ 原文层噪音大且与摘要重复，需要原话/案例细节时才开。
    """
    exclude_types = set(exclude_types or ())
    if not include_stars:
        exclude_types |= STAR_TYPES
    if not include_raw:
        exclude_types.add("raw")
    if backend == "bigram":
        candidates = bigram_search(query, top_k=10, exclude_types=exclude_types)
        results = _expand_with_neighbors(candidates, top_k=top_k + 5, exclude_types=exclude_types)[:top_k]
        return _enforce_type_filters(results, exclude_types)

    # Try Zvec first
    try:
        if not ZVEC_DB_DIR.exists() or not any(ZVEC_DB_DIR.iterdir()):
            raise RuntimeError("Zvec database not found")
        results = zvec_search(query, top_k=top_k, exclude_types=exclude_types)
    except Exception as e:
        print(f"[search] Zvec failed: {e}, falling back to bigram", file=sys.stderr)
        candidates = bigram_search(query, top_k=10, exclude_types=exclude_types)
        results = _expand_with_neighbors(candidates, top_k=top_k + 5, exclude_types=exclude_types)[:top_k]
    return _enforce_type_filters(results, exclude_types)


def main():
    # Windows 控制台默认 GBK：原文里的 emoji 会让 print 崩掉，强制 UTF-8 + replace
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        description="LLM Wiki Search: Zvec hybrid (FTS+Vector+RRF) with bigram fallback")
    parser.add_argument("query", help="Search query (supports CJK and English)")
    parser.add_argument("--top", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument("--backend", choices=["zvec", "bigram"], default="zvec",
                        help="Search backend: zvec (default) or bigram (fallback)")
    parser.add_argument("--exclude-type", action="append", default=[],
                        help="Page type(s) to exclude from results (e.g. --exclude-type github-star). Can be specified multiple times.")
    parser.add_argument("--include-raw", action="store_true",
                        help="把 raw/ 原文层纳入检索（默认排除：原文噪音大且与摘要重复，只在需要原话/细节时开）")
    parser.add_argument("--include-stars", action="store_true",
                        help="把 github-stars/ 收藏仓库纳入检索（默认排除：与自有知识物理隔离，查收藏项目请用 ghstars）")
    parser.add_argument("--similar", action="store_true",
                        help="Find documents similar to the given page path")
    parser.add_argument("--check-dup", action="store_true",
                        help="Check if the given page is a semantic duplicate")
    args = parser.parse_args()

    exclude_types = set(args.exclude_type) if args.exclude_type else set()
    # raw/ 原文层默认不参与检索（噪音大、与摘要重复）；需要原话时 --include-raw
    if not args.include_raw:
        exclude_types.add("raw")
    exclude_types = exclude_types or None

    if args.similar:
        results = similar_docs(args.query, top_k=args.top)
    elif args.check_dup:
        results = check_duplicate(args.query)
    else:
        results = search(args.query, top_k=args.top, backend=args.backend,
                         exclude_types=exclude_types,
                         include_stars=args.include_stars, include_raw=args.include_raw)

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
