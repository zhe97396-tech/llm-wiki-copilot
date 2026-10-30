#!/usr/bin/env python3
"""Batch embedding + Zvec ingestion for LLM Wiki RAG.

Scans wiki/ pages, generates embeddings via fastembed (local ONNX),
and upserts them into Zvec with FTS + vector indexes.

Usage:
    python scripts/embed.py                  # Full index (all wiki pages)
    python scripts/embed.py --incremental    # Only process changed pages
    python scripts/embed.py --file <path>    # Single file index
    python scripts/embed.py --stats          # Show Zvec stats
    python scripts/embed.py --rebuild        # Delete and rebuild entire index
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path

# Bootstrap
sys.path.insert(0, str(Path(__file__).parent))
from _utils import (
    DATA_ROOT, WIKI_DIR, RAW_DIR, DATA_DIR,
    read_file, write_file, strip_frontmatter,
    extract_frontmatter_field, all_wiki_pages, page_id, page_name, sha256,
    ensure_data_dir,
)
from zvec_config import (
    ZVEC_DB_DIR, EMBEDDING_MODEL, EMBEDDING_DIMENSION,
    FIELD_TITLE, FIELD_CONTENT, FIELD_CATEGORY, FIELD_TYPE,
    FIELD_SOURCE_PATH, FIELD_STATUS, VECTOR_FIELD,
    encode_doc_id, get_or_create_collection, load_embedding_model, embed_texts,
)

# ── Embed Cache ────────────────────────────────────────────────────────

EMBED_CACHE_FILE = DATA_DIR / "embed-cache.json"


def load_embed_cache() -> dict[str, str]:
    """Load embed cache: {page_id: sha256_of_content}."""
    if not EMBED_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(read_file(EMBED_CACHE_FILE))
    except (json.JSONDecodeError, ValueError):
        return {}


def save_embed_cache(cache: dict[str, str]) -> None:
    """Save embed cache."""
    ensure_data_dir()
    write_file(EMBED_CACHE_FILE, json.dumps(cache, ensure_ascii=False, indent=2))


# ── Page Extraction ────────────────────────────────────────────────────

def extract_page_data(page_path: Path) -> dict | None:
    """Extract indexable data from a wiki page.

    Returns dict with title, content, category, type, source_path, status
    or None if page should be skipped.
    """
    content = read_file(page_path)
    if not content.strip():
        return None

    body = strip_frontmatter(content)
    if len(body) < 50:  # Skip stubs
        return None

    title = extract_frontmatter_field(content, "title") or page_name(page_path)
    category = extract_frontmatter_field(content, "category") or ""
    page_type = extract_frontmatter_field(content, "type") or "source"
    # raw/ 是原文层，统一标 raw：便于检索时默认排除、需要时显式纳入
    if RAW_DIR in page_path.parents:
        page_type = "raw"
    status = extract_frontmatter_field(content, "status") or ""

    # source_path: relative path from repo root (for reverse lookup)
    source_path = str(page_path.relative_to(DATA_ROOT)).replace("\\", "/")

    return {
        "title": title,
        "content": body,
        "category": category,
        "page_type": page_type,
        "source_path": source_path,
        "status": status,
        # hash 整个文件（含 frontmatter）——type/category/status/title 变化也必须触发重索引，
        # 否则 #stars 归档（type→github-star-archived）等 frontmatter 变更不会被增量索引捕获
        "content_hash": sha256(content),
    }


# ── Indexing ──────────────────────────────────────────────────────────

def index_pages(page_paths: list[Path], incremental: bool = False) -> dict:
    """Index wiki pages into Zvec.

    Returns stats: {indexed, updated, skipped, errors}.
    """
    import zvec

    stats = {"indexed": 0, "updated": 0, "skipped": 0, "errors": 0}

    # Load cache for incremental mode
    cache = load_embed_cache() if incremental else {}
    # Keep a copy of original cache keys to distinguish new vs updated
    original_cache_keys = set(load_embed_cache().keys()) if not incremental else set(cache.keys())

    # Extract page data
    pages_data = []
    for p in page_paths:
        pid = page_id(p)
        data = extract_page_data(p)
        if data is None:
            stats["skipped"] += 1
            continue

        # Incremental: skip unchanged pages
        if incremental and pid in cache and cache[pid] == data["content_hash"]:
            stats["skipped"] += 1
            continue

        pages_data.append((pid, data))

    if not pages_data:
        print(f"No pages to index (skipped {stats['skipped']} unchanged)")
        return stats

    # Load embedding model (temporary)
    print(f"Loading embedding model: {EMBEDDING_MODEL}...")
    t0 = time.time()
    model = load_embedding_model()
    print(f"Model loaded in {time.time()-t0:.1f}s")

    # Generate embeddings in batch
    texts = [data["content"] for _, data in pages_data]
    print(f"Generating embeddings for {len(texts)} pages...")
    t1 = time.time()
    embeddings = embed_texts(model, texts)
    print(f"Embeddings generated in {time.time()-t1:.1f}s")

    # Release model memory
    del model
    gc.collect()
    print("Model released from memory")

    # Upsert to Zvec
    print("Upserting to Zvec...")
    coll = get_or_create_collection()

    for (pid, data), embedding in zip(pages_data, embeddings):
        doc_id = encode_doc_id(pid)
        try:
            coll.upsert(zvec.Doc(
                id=doc_id,
                fields={
                    FIELD_TITLE: data["title"],
                    FIELD_CONTENT: data["content"],
                    FIELD_CATEGORY: data["category"],
                    FIELD_TYPE: data["page_type"],
                    FIELD_SOURCE_PATH: data["source_path"],
                    FIELD_STATUS: data["status"],
                },
                vectors={VECTOR_FIELD: embedding},
            ))
            # Update cache
            cache[pid] = data["content_hash"]

            if pid in original_cache_keys:
                stats["updated"] += 1
            else:
                stats["indexed"] += 1
        except Exception as e:
            print(f"  ⚠️ Error indexing {pid}: {e}", file=sys.stderr)
            stats["errors"] += 1

    # Save cache
    save_embed_cache(cache)

    # Optimize collection to persist FTS index to disk
    # Zvec 0.5.1 bug: FTS index stays in memory after upsert; without optimize(),
    # the FTS index is lost when the process exits, making FTS queries return 0 results.
    print("Optimizing collection (persisting FTS index)...")
    try:
        coll.optimize()
        coll.flush()
        print("FTS index persisted ✅")
    except Exception as e:
        print(f"⚠️ Optimize failed (FTS may not work until next rebuild): {e}", file=sys.stderr)

    return stats


def index_single_file(file_path: str) -> dict:
    """Index a single wiki page by path.

    必须 resolve：extract_page_data 用 relative_to(REPO_ROOT) 算 source_path，
    传相对路径会 ValueError（只回 1 error、退出码仍 0）——
    表现就是「编译完了，但 MCP 搜不到新内容」。
    """
    p = Path(file_path).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if not p.exists():
        print(f"File not found: {file_path}", file=sys.stderr)
        return {"indexed": 0, "updated": 0, "skipped": 0, "errors": 1}
    return index_pages([p], incremental=True)


def show_stats() -> None:
    """Show Zvec collection statistics."""
    if not ZVEC_DB_DIR.exists() or not any(ZVEC_DB_DIR.iterdir()):
        print("Zvec database does not exist yet. Run embed.py first.")
        return

    coll = get_or_create_collection()
    print(f"Zvec Collection: {coll.schema.name}")
    print(f"  Doc count: {coll.stats.doc_count}")
    print(f"  DB path: {ZVEC_DB_DIR}")

    # Show cache info
    cache = load_embed_cache()
    print(f"  Embed cache: {len(cache)} pages tracked")

    if cache:
        print(f"  Cached pages (first 10):")
        for pid in sorted(cache.keys())[:10]:
            print(f"    {pid}")


def rebuild_index() -> None:
    """Delete existing index and rebuild from scratch."""
    import shutil

    # Ensure zvec is initialized with jieba_dict_dir BEFORE deleting and recreating
    from zvec_config import _ensure_zvec_init
    _ensure_zvec_init()

    if ZVEC_DB_DIR.exists():
        print(f"Deleting existing Zvec database: {ZVEC_DB_DIR}")
        shutil.rmtree(ZVEC_DB_DIR, ignore_errors=True)

    # Clear embed cache
    if EMBED_CACHE_FILE.exists():
        EMBED_CACHE_FILE.unlink()

    # Rebuild
    pages = all_wiki_pages()
    print(f"Rebuilding index from {len(pages)} wiki pages...")
    stats = index_pages(pages, incremental=False)
    print(f"✅ Rebuild complete: {stats['indexed']} indexed, {stats['errors']} errors")


# ── Main ───────────────────────────────────────────────────────────────

def _require_deps() -> None:
    """启动前检查关键依赖：缺失时给人话提示，而不是裸栈回溯。"""
    missing = []
    for mod, pkg in (("zvec", "zvec"), ("fastembed", "fastembed")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"❌ 缺少依赖：{', '.join(missing)}", file=sys.stderr)
        print("   安装：python -m pip install -r requirements.txt", file=sys.stderr)
        print("   Windows 建议用项目 venv：.venv/Scripts/python.exe scripts/embed.py ...", file=sys.stderr)
        print("   （首次运行还会自动下载约 241MB 向量模型）", file=sys.stderr)
        sys.exit(2)


def _write_embed_marker(stats: dict, mode: str) -> None:
    """记录本次索引时间：health / MCP stats 靠它判断索引是否落后（索引文件 mtime 会被只读打开刷新）。"""
    from datetime import datetime
    docs = None
    try:
        coll = get_or_create_collection()
        docs = int(coll.stats.doc_count)
    except Exception:
        pass
    payload = {"ts": time.time(), "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "docs": docs, "mode": mode,
               "indexed": stats.get("indexed"), "updated": stats.get("updated"),
               "skipped": stats.get("skipped"), "errors": stats.get("errors")}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "last_embed.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def main():
    # Windows 控制台默认 GBK：原文里的 emoji 会让 print 崩掉，强制 UTF-8 + replace
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _require_deps()
    parser = argparse.ArgumentParser(description="Embed wiki pages into Zvec for RAG search")
    parser.add_argument("--incremental", action="store_true",
                        help="Only index pages with changed content")
    parser.add_argument("--file", type=str, default=None,
                        help="Index a single file by path")
    parser.add_argument("--stats", action="store_true",
                        help="Show Zvec collection statistics")
    parser.add_argument("--rebuild", action="store_true",
                        help="Delete and rebuild entire index from scratch")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    if args.rebuild:
        empty = {"indexed": None, "updated": None, "skipped": None, "errors": 0}
        rebuild_index()
        _write_embed_marker(empty, "rebuild")
        return

    if args.file:
        stats = index_single_file(args.file)
        _write_embed_marker(stats, "file")
    else:
        pages = all_wiki_pages()
        print(f"Found {len(pages)} wiki pages")
        stats = index_pages(pages, incremental=args.incremental)
        _write_embed_marker(stats, "incremental" if args.incremental else "full")

    print(f"\n✅ Indexing complete: "
          f"{stats['indexed']} new, {stats['updated']} updated, "
          f"{stats['skipped']} skipped, {stats['errors']} errors")


if __name__ == "__main__":
    main()
