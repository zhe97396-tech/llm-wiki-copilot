#!/usr/bin/env python3
"""构建/更新**分块索引**（chunk index）——与整页索引并存，不替换它。

用法（vault 根目录，必须用 embed venv）：
    PYTHONPATH="" D:/Tools/wiki-embed-env/Scripts/python.exe scripts/embed_chunks.py --rebuild
    PYTHONPATH="" D:/Tools/wiki-embed-env/Scripts/python.exe scripts/embed_chunks.py --incremental
    PYTHONPATH="" D:/Tools/wiki-embed-env/Scripts/python.exe scripts/embed_chunks.py --stats

设计：
- 每页按 H2/H3 切块，块文本 = `页面标题 > 小标题\\n正文`（廉价版 contextual retrieval）
- 增量靠**块级 SHA-256**：只重嵌入变化的块，删除已消失的块
- 独立 DB 目录 `zvec_db_chunks/`，出问题不影响整页索引
"""

from __future__ import annotations

import gc
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _utils import (  # noqa: E402
    DATA_ROOT, all_wiki_pages, read_file, strip_frontmatter,
    extract_frontmatter_field, page_name, page_id,
)
try:  # 开源版 _utils 未提供 enable_utf8_stdout
    from _utils import enable_utf8_stdout
except ImportError:
    def enable_utf8_stdout() -> None:
        try:
            sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
        except Exception:
            pass


from zvec_chunk_config import (  # noqa: E402
    CHUNK_DB_DIR, CHUNK_CACHE_FILE,
    FIELD_PAGE_ID, FIELD_HEADING, FIELD_CONTENT, FIELD_CATEGORY,
    FIELD_TYPE, FIELD_SOURCE_PATH, FIELD_CHUNK_ID, VECTOR_FIELD,
    encode_chunk_id, split_page, get_or_create_chunk_collection,
    reset_chunk_collection, chunk_index_available,
)
from zvec_config import load_embedding_model, embed_texts  # noqa: E402

SKIP_NAMES = {"index.md", "overview.md"}   # 导航页不进分块索引（它们是目录，不是知识）


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cache() -> dict:
    try:
        return json.loads(CHUNK_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    CHUNK_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHUNK_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")


def collect_chunks() -> list[dict]:
    """扫描所有页面，切块并带上元数据。"""
    rows: list[dict] = []
    for p in all_wiki_pages():
        if "github-stars" in p.parts or p.name in SKIP_NAMES:
            continue
        try:
            content = read_file(p)
        except Exception:
            continue
        body = strip_frontmatter(content)
        if len(body) < 80:
            continue
        title = extract_frontmatter_field(content, "title") or page_name(p)
        category = extract_frontmatter_field(content, "category") or ""
        page_type = extract_frontmatter_field(content, "type") or "source"
        source_path = str(p.relative_to(DATA_ROOT)).replace("\\", "/")
        pid = page_id(p)
        for idx, (heading_path, text) in enumerate(split_page(title, body)):
            cid = f"{pid}#{idx}"
            rows.append({
                "chunk_id": cid,
                "page_id": pid,
                "heading": heading_path,
                "text": text,
                "category": category,
                "page_type": page_type,
                "source_path": source_path,
                "hash": sha(text),
            })
    return rows


def main() -> int:
    argv = sys.argv[1:]
    mode = "incremental"
    if "--rebuild" in argv:
        mode = "rebuild"
    elif "--stats" in argv:
        mode = "stats"
    elif "--incremental" in argv:
        mode = "incremental"

    if mode == "stats":
        if not chunk_index_available():
            print("分块索引不存在（先跑 --rebuild）")
            return 0
        coll = get_or_create_chunk_collection()
        cache = load_cache()
        print(f"分块索引：{coll.stats.doc_count} 块；缓存记录 {len(cache)} 块；路径 {CHUNK_DB_DIR}")
        return 0

    t0 = time.time()
    rows = collect_chunks()
    print(f"切块完成：{len(rows)} 块（{len({r['page_id'] for r in rows})} 个页面）")

    if mode == "rebuild":
        if CHUNK_DB_DIR.exists():
            print(f"删除旧分块索引：{CHUNK_DB_DIR}")
            reset_chunk_collection()
            shutil.rmtree(CHUNK_DB_DIR, ignore_errors=True)
        cache = {}
    else:
        cache = load_cache()

    todo = [r for r in rows if cache.get(r["chunk_id"]) != r["hash"]]
    alive = {r["chunk_id"] for r in rows}
    stale = [cid for cid in cache if cid not in alive]
    print(f"待嵌入 {len(todo)} 块；待删除 {len(stale)} 块（已消失的小节）")

    import zvec

    coll = get_or_create_chunk_collection()
    if coll is None:
        print("❌ 分块索引打开失败", file=sys.stderr)
        return 2

    # 索引被删/损坏的自愈：缓存说 1886 块、库里只剩几十块时，纯增量会永远补不回来
    if mode != "rebuild" and cache:
        try:
            live = int(coll.stats.doc_count)
        except Exception:
            live = len(cache)
        if live * 2 < len(cache):
            print(f"⚠️ 分块索引只有 {live} 块，但缓存记录 {len(cache)} 块（索引被删或损坏）→ 改为全量重建")
            cache = {}
            todo = rows
            stale = []

    # 删除过期块
    if stale:
        try:
            coll.delete([encode_chunk_id(cid) for cid in stale])
            for cid in stale:
                cache.pop(cid, None)
            print(f"已删除 {len(stale)} 个过期块")
        except Exception as exc:
            print(f"⚠️ 删除过期块失败：{exc}", file=sys.stderr)

    stats = {"indexed": 0, "updated": 0, "errors": 0}
    if todo:
        model = load_embedding_model()
        print(f"加载模型 → 生成 {len(todo)} 个块向量…")
        t1 = time.time()
        vectors = embed_texts(model, [r["text"] for r in todo])
        del model
        gc.collect()
        print(f"向量生成完成 {time.time()-t1:.1f}s（模型已释放）")

        for r, vec in zip(todo, vectors):
            try:
                coll.upsert(zvec.Doc(
                    id=encode_chunk_id(r["chunk_id"]),
                    fields={
                        FIELD_PAGE_ID: r["page_id"],
                        FIELD_CHUNK_ID: r["chunk_id"],
                        FIELD_HEADING: r["heading"],
                        FIELD_CONTENT: r["text"],
                        FIELD_CATEGORY: r["category"],
                        FIELD_TYPE: r["page_type"],
                        FIELD_SOURCE_PATH: r["source_path"],
                    },
                    vectors={VECTOR_FIELD: vec},
                ))
                existed = r["chunk_id"] in cache
                cache[r["chunk_id"]] = r["hash"]
                stats["updated" if existed else "indexed"] += 1
            except Exception as exc:
                print(f"  ⚠️ 块写入失败 {r['chunk_id']}: {exc}", file=sys.stderr)
                stats["errors"] += 1

    save_cache(cache)

    # Zvec 0.5.1：FTS 索引在内存里，不 optimize/flush 的话进程退出就丢
    print("持久化 FTS 索引…")
    try:
        coll.optimize()
        coll.flush()
        print("FTS 已持久化 ✅")
    except Exception as exc:
        print(f"⚠️ optimize 失败（FTS 可能需重建）：{exc}", file=sys.stderr)

    print(f"✅ 分块索引完成：{stats['indexed']} new / {stats['updated']} updated / "
          f"{stats['errors']} errors / 共 {coll.stats.doc_count} 块 / {time.time()-t0:.1f}s")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    enable_utf8_stdout()
    sys.exit(main())
