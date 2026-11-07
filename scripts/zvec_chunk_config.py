#!/usr/bin/env python3
"""分块索引（chunk index）的 Zvec 配置 —— 与整页索引**物理隔离**。

为什么独立库：
1. 整页索引（`zvec_db/`）是生产链路，不能因为分块实验被破坏；两个 DB 目录互不影响，
   出问题把 `LLM_WIKI_CHUNK_SEARCH=off` 一关就回到原状。
2. Zvec 的 LOCK 是**按 DB 目录**跨进程独占的，放在同一个目录里会互相抢锁。

设计依据（2026-11-07 实测）：同一 embedding 模型下，
整页一个向量 hit@1 20% → 按 H2/H3 分块后 hit@1 40%（hit@3 45%→60%），
所以分块是「新增召回源」，不是替换整页索引。

块文本 = `页面标题 > 小标题\n正文`，这是最廉价的 contextual retrieval
（块离开页面时仍带着自己的位置语义，Anthropic 那套是让 LLM 写一段上下文，我们先用标题路径）。
"""

from __future__ import annotations

import gc
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _utils import DATA_DIR  # noqa: E402

# ── 存储位置（独立于整页索引）────────────────────────────────────────────

CHUNK_DB_DIR = DATA_DIR / "zvec_db_chunks"
CHUNK_COLLECTION_NAME = "llm_wiki_chunks"
CHUNK_CACHE_FILE = DATA_DIR / "chunk_cache.json"

# ── 字段名 ──────────────────────────────────────────────────────────────

FIELD_PAGE_ID = "page_id"        # 所属页面 id（concepts/xxx、summaries/…）
FIELD_HEADING = "heading"        # 小标题路径
FIELD_CONTENT = "content"        # 块正文（含标题前缀），参与 FTS
FIELD_CATEGORY = "category"
FIELD_TYPE = "page_type"
FIELD_SOURCE_PATH = "source_path"
FIELD_CHUNK_ID = "chunk_id"      # f"{page_id}#{index}"
VECTOR_FIELD = "embedding"

# ── 切块参数（2026-11-07 实测：中位 250 字，4% 超 1200 字）──────────────

MIN_CHUNK_CHARS = 80            # 太短的块（孤立标题、空段）不建
MAX_CHUNK_CHARS = 1500          # 单块上限，超了在句末截断（embedding 模型窗口有限）
CHUNK_FETCH_TOPK = 40           # 检索时从块索引取多少候选
CHUNK_RRF_WEIGHT = 1.0          # 块召回在 RRF 融合里的权重（1.0 = 与整页等权）

_HEADING_RE = re.compile(r"^\s*(#{2,3})\s*(.+)$", re.M)


def encode_chunk_id(chunk_id: str) -> str:
    """chunk_id → Zvec 可用的 ASCII doc id（Zvec 不支持中文 id）。"""
    return "ck_" + hashlib.md5(chunk_id.encode("utf-8")).hexdigest()[:8]


def split_page(title: str, body: str) -> list[tuple[str, str]]:
    """按 H2/H3 把一个页面切成 [(heading_path, chunk_text), ...]。

    H1（页面标题行）不参与切分；每个块的正文前面拼 `页面 > 小节` 路径。
    """
    out: list[tuple[str, str]] = []
    parts = re.split(r"\n(?=#{2,3} )", "\n" + body)
    for idx, part in enumerate(parts):
        part = part.strip("\n")
        if len(part.strip()) < MIN_CHUNK_CHARS:
            continue
        m = _HEADING_RE.search(part)
        if m:
            heading = m.group(2).strip()
            text_body = part[m.end():].strip()
        else:
            heading = "开头"
            text_body = part.strip()
        if len(text_body) < MIN_CHUNK_CHARS:
            continue
        if len(text_body) > MAX_CHUNK_CHARS:
            cut = text_body.rfind("。", 0, MAX_CHUNK_CHARS)
            text_body = text_body[: cut + 1] if cut > MIN_CHUNK_CHARS else text_body[:MAX_CHUNK_CHARS]
        heading_path = f"{title} > {heading}"
        out.append((heading_path, f"{heading_path}\n{text_body}"))
    return out


def get_chunk_schema():
    """分块集合的 schema（字段与整页库刻意不同：多 page_id / heading / chunk_id）。

    维度从 zvec_config.EMBEDDING_DIMENSION 取，不写死 384 —— 换模型时两库不会错位。
    """
    import zvec
    from zvec_config import EMBEDDING_DIMENSION

    return zvec.CollectionSchema(
        name=CHUNK_COLLECTION_NAME,
        fields=[
            zvec.FieldSchema(FIELD_PAGE_ID, zvec.DataType.STRING, nullable=False),
            zvec.FieldSchema(FIELD_CHUNK_ID, zvec.DataType.STRING, nullable=False),
            zvec.FieldSchema(FIELD_HEADING, zvec.DataType.STRING, nullable=True),
            zvec.FieldSchema(
                FIELD_CONTENT,
                zvec.DataType.STRING,
                nullable=False,
                index_param=zvec.FtsIndexParam(tokenizer_name="jieba", filters=["lowercase"]),
            ),
            zvec.FieldSchema(FIELD_CATEGORY, zvec.DataType.STRING, nullable=True),
            zvec.FieldSchema(FIELD_TYPE, zvec.DataType.STRING, nullable=True),
            zvec.FieldSchema(FIELD_SOURCE_PATH, zvec.DataType.STRING, nullable=True),
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


_chunk_cached = None


def _readonly() -> bool:
    return os.environ.get("LLM_WIKI_ZVEC_READONLY", "").strip().lower() in ("1", "true", "yes", "on")


def reset_chunk_collection() -> None:
    """释放分块索引句柄（让出 Zvec 的跨目录文件锁）。"""
    global _chunk_cached
    if _chunk_cached is None:
        return
    _chunk_cached = None
    gc.collect()


def chunk_index_available() -> bool:
    try:
        return CHUNK_DB_DIR.exists() and any(CHUNK_DB_DIR.iterdir())
    except Exception:
        return False


def get_or_create_chunk_collection():
    """打开（或创建）分块集合。只读模式下不存在就返回 None（分块检索静默关闭）。"""
    global _chunk_cached
    import zvec
    from zvec_config import _ensure_zvec_init, _collection_option

    if _chunk_cached is not None:
        return _chunk_cached
    _ensure_zvec_init()
    path = str(CHUNK_DB_DIR)

    if chunk_index_available():
        try:
            if _readonly():
                _chunk_cached = zvec.open(path=path, option=_collection_option(zvec, readonly=True))
            else:
                _chunk_cached = zvec.open(path=path)
            return _chunk_cached
        except Exception:
            if _readonly():
                return None
    if _readonly():
        return None
    CHUNK_DB_DIR.parent.mkdir(parents=True, exist_ok=True)
    _chunk_cached = zvec.create_and_open(path=path, schema=get_chunk_schema())
    return _chunk_cached


if __name__ == "__main__":
    print(f"CHUNK_DB_DIR: {CHUNK_DB_DIR}")
    print(f"available: {chunk_index_available()}")
    print(f"encode_chunk_id('concepts/example#0') = {encode_chunk_id('concepts/example#0')}")
