#!/usr/bin/env python3
"""Zvec + fastembed configuration for LLM Wiki RAG.

Centralizes: Zvec schema, embedding model config, doc ID encoding,
collection lifecycle, and search defaults.
"""

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


def get_or_create_collection() -> "zvec.Collection":
    """Get existing Zvec collection or create new one.

    Returns a cached instance if one is already open in this process,
    avoiding Zvec's file-lock conflict on repeated opens.
    """
    global _cached_collection
    import zvec

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
            _cached_collection = zvec.open(path=db_path)
            return _cached_collection
        except Exception:
            pass  # Corrupted or wrong schema — rebuild

    # Create new collection
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
    print("=== zvec_config.py self-test ===")
    print(f"ZVEC_DB_DIR: {ZVEC_DB_DIR}")
    print(f"EMBEDDING_MODEL: {EMBEDDING_MODEL}")
    print(f"EMBEDDING_DIMENSION: {EMBEDDING_DIMENSION}")
    print(f"encode_doc_id('concepts/认知Commons'): {encode_doc_id('concepts/认知Commons')}")
    print(f"encode_doc_id('summaries/AI & 技术/2026-07-10_video_Karpathy'): {encode_doc_id('summaries/AI & 技术/2026-07-10_video_Karpathy')}")
    print("=== All tests passed ===")
