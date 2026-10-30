#!/usr/bin/env python3
"""Zvec + fastembed configuration for LLM Wiki RAG.

Centralizes: Zvec schema, embedding model config, doc ID encoding,
collection lifecycle, and search defaults.
"""

import gc
from __future__ import annotations

import hashlib
from pathlib import Path
import sys

# Bootstrap shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from _utils import REPO_ROOT, DATA_DIR, ensure_data_dir

# ── Zvec Storage ────────────────────────────────────────────────────────

ZVEC_DB_DIR = DATA_DIR / "zvec_db"
ZVEC_COLLECTION_NAME = "llm_wiki"

# ── Embedding Config ────────────────────────────────────────────────────

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSION = 384  # multilingual-MiniLM-L12 output dimension

# fastembed 模型缓存放在数据目录内（2026-08-01 起曾被临时目录清理导致重新下载 241MB）
# 默认位置：<data_root>/.llm-wiki/fastembed_cache，可用 config.paths.fastembed_cache 覆盖
import os as _os
try:
    from config import get as _cfg_get
    _cache_cfg = _cfg_get("paths.fastembed_cache")
except ImportError:
    _cache_cfg = None
_PERSIST_CACHE = str(_cache_cfg) if _cache_cfg else str(DATA_DIR / "fastembed_cache")
_os.environ.setdefault("FASTEMBED_CACHE_PATH", _PERSIST_CACHE)
_os.environ.setdefault("HF_HOME", _PERSIST_CACHE)

# ── Schema Field Names ──────────────────────────────────────────────────

FIELD_TITLE = "title"
FIELD_CONTENT = "content"
FIELD_CATEGORY = "category"
FIELD_TYPE = "page_type"  # concept/source/synthesis/entity
FIELD_SOURCE_PATH = "source_path"
FIELD_STATUS = "status"
VECTOR_FIELD = "embedding"

# ── Search Defaults ────────────────────────────────────────────────────

DEFAULT_TOPK = 5
RRF_RANK_CONSTANT = 60

# ── Doc ID Encoding ────────────────────────────────────────────────────
# Zvec doc id does not support CJK characters; use stable ASCII hash.


def encode_doc_id(page_id_str: str) -> str:
    """Encode a page_id (may contain CJK) to ASCII-safe Zvec doc id.

    Uses MD5 hash prefix for stability and uniqueness.
    Example: 'concepts/认知Commons' -> 'doc_a1b2c3d4'
    """
    h = hashlib.md5(page_id_str.encode("utf-8")).hexdigest()[:8]
    return f"doc_{h}"


# ── Zvec Schema Definition ─────────────────────────────────────────────


def get_zvec_schema() -> "zvec.CollectionSchema":
    """Return the Zvec collection schema for LLM Wiki."""
    import zvec

    return zvec.CollectionSchema(
        name=ZVEC_COLLECTION_NAME,
        fields=[
            zvec.FieldSchema(FIELD_TITLE, zvec.DataType.STRING, nullable=False),
            zvec.FieldSchema(
                FIELD_CONTENT,
                zvec.DataType.STRING,
                nullable=False,
                index_param=zvec.FtsIndexParam(
                    tokenizer_name="jieba",
                    filters=["lowercase"],
                ),
            ),
            zvec.FieldSchema(FIELD_CATEGORY, zvec.DataType.STRING, nullable=True),
            zvec.FieldSchema(FIELD_TYPE, zvec.DataType.STRING, nullable=True),
            zvec.FieldSchema(FIELD_SOURCE_PATH, zvec.DataType.STRING, nullable=False),
            zvec.FieldSchema(FIELD_STATUS, zvec.DataType.STRING, nullable=True),
        ],
        vectors=[
            zvec.VectorSchema(
                VECTOR_FIELD,
                zvec.DataType.VECTOR_FP32,
                dimension=EMBEDDING_DIMENSION,
                index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
            ),
        ],
    )


def _ensure_zvec_init():
    """Initialize Zvec with jieba dict path for CJK FTS support.
    
    Must be called before any zvec operations. Safe to call multiple times
    (subsequent calls are no-ops since zvec.init() can only run once).
    """
    import zvec
    
    try:
        # Auto-detect jieba dict from zvec package location
        zvec_pkg_dir = Path(zvec.__file__).parent
        jieba_dict_dir = zvec_pkg_dir / "data" / "jieba_dict"
        if jieba_dict_dir.exists():
            zvec.init(jieba_dict_dir=str(jieba_dict_dir))
        else:
            zvec.init()
    except RuntimeError:
        pass  # Already initialized


# Module-level cache for the Zvec collection instance.
# Zvec uses a file lock (LOCK) per DB path — only one Collection can be open
# at a time within the same process. Caching avoids "Can't lock read-write
# collection" errors when multiple functions call get_or_create_collection().
_cached_collection = None


def _readonly_requested() -> bool:
    """长期存活的只读进程（MCP 服务等）用：`LLM_WIKI_ZVEC_READONLY=1`。

    Zvec 的写锁是**跨进程独占**的（DB 目录下的 LOCK 文件）：任何进程用写模式打开索引，
    `embed.py` 就会失败（`Can't lock read-write collection`）。MCP 是常驻子进程，
    必须只读打开，否则一用 MCP 索引就再也没法更新。
    """
    return _os.environ.get("LLM_WIKI_ZVEC_READONLY", "").strip().lower() in ("1", "true", "yes", "on")


def _collection_option(zvec_mod, readonly: bool):
    """zvec.open 的 option 必须是 CollectionOption 实例（传 dict 会 TypeError）。"""
    values = {"enable_mmap": 1, "read_only": 1 if readonly else 0}
    try:
        return zvec_mod.CollectionOption(**values)
    except Exception:
        return values


def reset_collection() -> None:
    """释放索引句柄（drop 引用 + gc），让出 Zvec 的跨进程文件锁。

    常驻进程（MCP）只要还持有 Collection，另一个进程的 embed.py 就打不开索引
    （`zvec.open` 失败 → 退回 create → `path validate failed: … exists`）。
    必须在每次工具调用结束后调用，否则索引会永久停在旧版本。
    """
    global _cached_collection
    if _cached_collection is None:
        return
    _cached_collection = None
    gc.collect()


def get_or_create_collection() -> "zvec.Collection":
    """Get existing Zvec collection or create new one.

    Returns a cached instance if one is already open in this process,
    avoiding Zvec's file-lock conflict on repeated opens.

    只读模式（`LLM_WIKI_ZVEC_READONLY=1`）下永不创建、不加写锁。
    """
    global _cached_collection
    import zvec
    readonly = _readonly_requested()

    if _cached_collection is not None:
        try:
            # Verify the cached collection is still usable
            _ = _cached_collection.stats.doc_count
            return _cached_collection
        except Exception:
            _cached_collection = None

    _ensure_zvec_init()

    db_path = str(ZVEC_DB_DIR)

    # Try to open existing collection first
    if ZVEC_DB_DIR.exists() and any(ZVEC_DB_DIR.iterdir()):
        try:
            if readonly:
                _cached_collection = zvec.open(path=db_path, option=_collection_option(zvec, readonly=True))
            else:
                _cached_collection = zvec.open(path=db_path)
            return _cached_collection
        except Exception as exc:
            hint = str(exc).lower()
            if readonly:
                raise RuntimeError(
                    f"只读打开 Zvec 索引失败：{db_path}（LLM_WIKI_ZVEC_READONLY=1 时不自动重建，"
                    f"请先运行 scripts/embed.py）"
                )
            if "exist" in hint or "lock" in hint:
                raise RuntimeError(
                    f"打不开 Zvec 索引（{db_path}）：{exc}\n"
                    f"通常是另一个进程还持有索引句柄（常驻的 wiki MCP 进程）。"
                    f"让它释放（新版 MCP 已每次调用后释放；旧版需重启客户端/网关）后重跑 embed.py。"
                ) from exc
            pass  # 确认损坏/结构不符 → 重建

    # Create new collection
    if readonly:
        raise RuntimeError(
            f"只读模式（LLM_WIKI_ZVEC_READONLY=1）不会创建索引：{db_path} 不存在；请先运行 scripts/embed.py"
        )
    ZVEC_DB_DIR.parent.mkdir(parents=True, exist_ok=True)
    schema = get_zvec_schema()
    _cached_collection = zvec.create_and_open(path=db_path, schema=schema)
    return _cached_collection


# ── Embedding Helper ───────────────────────────────────────────────────


def load_embedding_model():
    """Load fastembed model (temporary — caller must del + gc.collect after use)."""
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=EMBEDDING_MODEL)


def embed_texts(model, texts: list[str]) -> list[list[float]]:
    """Embed a list of texts using the loaded model. Returns list of float vectors."""
    import numpy as np
    embeddings = list(model.embed(texts))
    return [e.tolist() if isinstance(e, np.ndarray) else list(e) for e in embeddings]


# ── Self-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Windows 管道/控制台默认 GBK：emoji 会让 print 崩掉（工作其实已完成）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=== zvec_config.py self-test ===")
    print(f"ZVEC_DB_DIR: {ZVEC_DB_DIR}")
    print(f"EMBEDDING_MODEL: {EMBEDDING_MODEL}")
    print(f"EMBEDDING_DIMENSION: {EMBEDDING_DIMENSION}")
    print(f"encode_doc_id('concepts/认知Commons'): {encode_doc_id('concepts/认知Commons')}")
    print(f"encode_doc_id('summaries/AI & 技术/2026-07-10_video_Karpathy'): {encode_doc_id('summaries/AI & 技术/2026-07-10_video_Karpathy')}")
    print("=== All tests passed ===")
