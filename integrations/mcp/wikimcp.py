#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM Wiki MCP Server —— 把本项目的知识库开放给任意 MCP 客户端。

设计要点
--------
* **零第三方依赖**：只用标准库 + 本仓库 `scripts/`（search.py / ask.py）。
  但运行解释器必须是装了 zvec + fastembed 的那个 venv（和 `scripts/embed.py` 同一个），
  否则会静默降级成 bigram 关键词检索、语义能力失真。
* **stdio 传输**：由客户端拉起子进程，无端口、无守护进程、无鉴权，只在本机可用。
* **与主链路一致**：复用同一份 Zvec 索引 + 同一套证据分层排序 + 双链/实体搭桥，
  并额外暴露 ask（复杂度 / 覆盖度 / 缺口）。
* **范围可控**：
    scope=personal（默认）—— 全库
    scope=team          —— 只给 wiki/concepts + wiki/summaries + wiki/evergreen
                            （排除 conversations / topics / github-stars / system）

知识库位置：由 `_utils` 解析（`LLM_WIKI_DATA`，或仓库下的 `vault/`；demo 可用
`LLM_WIKI_DATA=demo`）。引擎固定在仓库 `scripts/`。

协议：JSON-RPC 2.0 over stdio（MCP 2024-11-05 / 2025-06-18 兼容）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]   # integrations/mcp/wikimcp.py → 仓库根
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _utils import DATA_ROOT  # noqa: E402

VAULT = Path(DATA_ROOT).resolve()

# ⚠️ MCP 是常驻进程：必须以只读模式打开 Zvec（不加写锁），否则 embed.py 会因
# "Can't lock read-write collection" 失败，索引从此无法更新。
os.environ.setdefault("LLM_WIKI_ZVEC_READONLY", "1")

# ── stdout 保护：协议必须独占 stdout，脚本里的任何 print 都改道 stderr ──
_PROTO_OUT = os.fdopen(os.dup(1), "wb", buffering=0)
sys.stdout = sys.stderr
os.dup2(2, 1)  # 让 C 扩展/子进程写 fd1 的内容也落到 stderr

SERVER_NAME = "llm-wiki"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2025-06-18"

TEAM_TYPES = {"github-star", "github-star-archived", "topic", "distill"}
TEAM_PREFIXES = ("concepts/", "summaries/", "evergreen/")


# ── 延迟导入知识库模块（模型加载慢，只在真正调用时初始化） ──────────────
_state: dict = {}


def _mods():
    if "search" not in _state:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import search as search_mod  # noqa: E402
        import ask as ask_mod        # noqa: E402
        import _utils                # noqa: E402

        _state["search"] = search_mod
        _state["ask"] = ask_mod
        _state["utils"] = _utils
        _state["pages"] = {_utils.page_id(p): p for p in _utils.all_wiki_pages()}
    return _state


def _pages(refresh: bool = False) -> dict[str, Path]:
    """页面 id → 路径。缓存只在进程内；编译出新页面后必须能刷新，否则 fetch 打不开。"""
    mods = _mods()
    if refresh:
        mods["pages"] = {mods["utils"].page_id(p): p for p in mods["utils"].all_wiki_pages()}
    return mods["pages"]


def _scope_filter(results: list[dict], scope: str) -> list[dict]:
    """team 范围再做一次路径白名单兜底（类型过滤之外的双保险）。"""
    if scope != "team":
        return results
    return [r for r in results if str(r.get("path", "")).startswith("wiki/")
            and any(r["path"].split("wiki/", 1)[-1].startswith(p) for p in TEAM_PREFIXES)]


def _exclude_types(scope: str) -> set[str] | None:
    """scope 层面的硬隔离（team 只出知识本体）。

    raw/ 的默认排除已下沉到 search() 内部（include_raw）；github-stars/ 根本不进索引。
    """
    return set(TEAM_TYPES) if scope == "team" else None


def _frontmatter(path: Path) -> dict:
    utils = _mods()["utils"]
    text = path.read_text(encoding="utf-8", errors="ignore")
    fm: dict = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


# ── 工具实现 ────────────────────────────────────────────────────────────
def tool_search(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    top_k = int(args.get("top_k") or 5)
    top_k = max(1, min(top_k, 20))
    scope = (args.get("scope") or "personal").lower()

    s = _mods()["search"]
    results = s.search(query, top_k=top_k,
                       exclude_types=_exclude_types(scope),
                       include_raw=bool(args.get("include_raw")))
    results = _scope_filter(results, scope)

    out = []
    for r in results:
        item = {
            "id": r.get("page_id"),
            "title": r.get("title"),
            "category": r.get("category") or "",
            "type": r.get("type"),
            "confidence": r.get("confidence"),
            "source_type": "concept" if str(r.get("page_id", "")).startswith("concepts/")
            else ("summary" if str(r.get("page_id", "")).startswith("summaries/") else r.get("type")),
            "snippet": r.get("snippet"),
        }
        if r.get("neighbors"):
            item["related"] = r["neighbors"][:5]
        if r.get("expanded_from"):
            item["expanded_from"] = r["expanded_from"]
        out.append(item)
    return {"query": query, "scope": scope, "count": len(out), "results": out}


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _outline(text: str, per_section: int = 90, max_chars: int = 900) -> str:
    """把整篇压成「标题 + 每节首句」的大纲：先判相关性，再决定要不要取全文。"""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, ln in enumerate(lines[1:], 1):
            if ln.strip() == "---":
                lines = lines[i + 1:]
                break
    out: list[str] = []
    take = True
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        m = _HEADING_RE.match(s)
        if m:
            out.append(f"{m.group(1)} {m.group(2)}")
            take = True
            continue
        if not take:
            continue
        if s.startswith((">", "|", "!", "```")):
            continue
        out.append("  " + s[:per_section] + ("…" if len(s) > per_section else ""))
        take = False
    if len(out) < 3:  # 无标题的短页：退化成前几行
        out = ["  " + x.strip()[:per_section] for x in lines if x.strip()][:3]
    text_out = "\n".join(out)
    if len(text_out) > max_chars:
        text_out = text_out[:max_chars].rstrip() + "\n…（大纲已截断）"
    return text_out


def _index_stamp() -> tuple[float, str, int | None]:
    """索引「跑完的时间」：优先读 embed 写的标记文件，退化到索引文件 mtime（只读打开会刷新 mtime）。"""
    marker = VAULT / ".llm-wiki" / "last_embed.json"
    if marker.exists():
        try:
            d = json.loads(marker.read_text(encoding="utf-8"))
            ts = float(d.get("ts") or 0)
            if ts:
                return ts, "last_embed.json", d.get("docs")
        except Exception:
            pass
    db = VAULT / ".llm-wiki" / "zvec_db"
    idx = 0.0
    if db.exists():
        for x in db.rglob("*"):
            try:
                if x.is_file() and x.name != "LOCK":
                    idx = max(idx, x.stat().st_mtime)
            except OSError:
                pass
    return idx, "index mtime", None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def _index_freshness(pages: dict[str, Path]) -> dict:
    """索引是否落后于 Markdown：页面 mtime vs 上次 embed 时间 + 文档数对账。"""
    idx_ts, source, docs = _index_stamp()
    newer, newest = [], 0.0
    for path in pages.values():
        try:
            m = path.stat().st_mtime
        except OSError:
            continue
        newest = max(newest, m)
        if idx_ts and m > idx_ts + 60:
            newer.append(path.name)
    stale = len(newer) >= 3 or (len(newer) >= 1 and idx_ts and (time.time() - idx_ts) > 86400)
    missing = None
    if isinstance(docs, int) and docs >= 0:
        missing = len(pages) - docs
        if missing > 0:
            stale = True
    return {
        "index_updated": _iso(idx_ts),
        "newest_page": _iso(newest),
        "pages_not_indexed": len(newer),
        "docs_missing": missing,
        "stamp_source": source,
        "stale": bool(stale),
        "hint": ("索引落后：跑 python scripts/embed.py --incremental"
                 if (newer or (missing or 0) > 0) else "索引与文件同步"),
    }


def tool_fetch(args: dict) -> dict:
    pid = (args.get("id") or "").strip().lstrip("/")
    if not pid:
        raise ValueError("id 不能为空")
    include_raw = bool(args.get("include_raw"))

    # 1) 索引内页面（concepts/xxx、summaries/分类/xxx …）
    key = pid.replace(".md", "")
    path = _pages().get(key)
    if path is None:  # 进程启动后才编译出来的页面：重扫一次再找
        path = _pages(refresh=True).get(key)

    # 2) 允许显式取 raw/（原文层，按路径直达）
    if path is None and include_raw and pid.startswith("raw/"):
        cand = (VAULT / pid).with_suffix(".md") if not pid.endswith(".md") else VAULT / pid
        if cand.exists() and VAULT / "raw" in cand.parents:
            path = cand

    if path is None or not path.exists():
        raise ValueError(f"未找到页面：{pid}（索引内 id 形如 concepts/精准定位）")

    text = path.read_text(encoding="utf-8", errors="ignore")
    fm = _frontmatter(path) if not pid.startswith("raw/") else {}
    # 字段白名单：不外发 interaction_count / intent / marketing_ratio / status
    keep = ("title", "category", "date", "source", "type")
    total = len(text)
    mode = (args.get("mode") or "full").strip().lower()
    try:
        max_chars = int(args.get("max_chars") or 0)
    except (TypeError, ValueError):
        max_chars = 0

    payload = {
        "id": pid,
        "path": str(path.relative_to(VAULT)).replace("\\", "/"),
        "frontmatter": {k: fm[k] for k in keep if k in fm},
        "mode": "full",
        "total_chars": total,
    }
    if mode == "outline":
        body = _outline(text)
        payload.update({"mode": "outline", "body": body, "chars": len(body),
                        "hint": "大纲用于判断相关性；确认相关后传 mode='full' 取全文"})
    elif max_chars > 0 and total > max_chars:
        payload.update({"body": text[:max_chars].rstrip() + "\n\n…（已截断）",
                        "chars": max_chars, "truncated": True})
    else:
        payload.update({"body": text, "chars": total})
    return payload


def tool_list_concepts(args: dict) -> dict:
    mods = _mods()
    cat = (args.get("category") or "").strip()
    items = []
    for pid, path in mods["pages"].items():
        if not pid.startswith("concepts/"):
            continue
        fm = _frontmatter(path)
        if cat and fm.get("category") != cat:
            continue
        body = path.read_text(encoding="utf-8", errors="ignore")
        # 取正文第一段非标题文本作为一句话定义
        definition = ""
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("---") or line.startswith("#") or line.startswith("[["):
                continue
            definition = line[:180]
            break
        items.append({
            "id": pid,
            "name": pid.split("/", 1)[1],
            "category": fm.get("category", ""),
            "definition": definition,
        })
    items.sort(key=lambda x: (x["category"], x["name"]))
    return {"count": len(items), "concepts": items}


def tool_stats(_: dict) -> dict:
    pages = _pages(refresh=True)  # 统计要反映磁盘现状（编译后不必重启进程）
    by_dir: dict[str, int] = {}
    for _pid, path in pages.items():
        rel = str(path.relative_to(VAULT)).replace("\\", "/")
        parts = rel.split("/")
        key = "/".join(parts[:2]) if parts[0] == "wiki" else parts[0]
        by_dir[key] = by_dir.get(key, 0) + 1

    index_docs = None
    try:
        from zvec_config import get_or_create_collection  # type: ignore
        index_docs = int(get_or_create_collection().stats.doc_count)
    except Exception:
        pass

    fresh = _index_freshness(pages)
    payload = {
        "vault": str(VAULT),
        "pages": len(pages),
        "index_docs": index_docs,
        "by_dir": dict(sorted(by_dir.items(), key=lambda kv: -kv[1])),
        "index_freshness": fresh,
        "scopes": {
            "personal": "全库：wiki（含 conversations）+ raw + media_output/topics；不含 github-stars（独立库）",
            "team": "仅 wiki/concepts + wiki/summaries + wiki/evergreen",
        },
        "note": "raw/ 已进索引但默认不参与检索：要原话/细节传 include_raw=true，或 fetch(id='raw/…')。github-stars 不在本库，收藏项目用 ghstars。",
    }
    if fresh["stale"]:
        payload["warning"] = (f"索引落后 {fresh['pages_not_indexed']} 篇 / 缺 {fresh['docs_missing']} 篇"
                              f"（更新于 {fresh['index_updated']}）——{fresh['hint']}")
    return payload


def tool_ask(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    scope = (args.get("scope") or "personal").lower()
    a = _mods()["ask"]
    res = a.ask(query, exclude_types=_exclude_types(scope))
    if scope == "team":
        res["results"] = _scope_filter(res.get("results", []), scope)
    return res


TOOLS = [
    {
        "name": "search",
        "description": (
            "检索本地 LLM Wiki 知识库（Zvec 混合检索：jieba 全文 + 向量语义 + RRF，"
            "证据分层排序）。返回概念页/总结页的 id、标题、分类、置信度与摘要片段。"
            "要读全文用 fetch。scope=personal 全库（不含收藏仓库）；scope=team 仅知识本体；"
            "include_raw=true 才检索 raw/ 原文层（默认关）。"
            "本库只含个人知识，不含收藏仓库——查 star 过的项目请用 ghstars。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词或问题，支持中英文"},
                "top_k": {"type": "integer", "description": "返回条数，1-20，默认 5"},
                "scope": {"type": "string", "enum": ["personal", "team"],
                          "description": "检索范围，默认 personal（全库）"},
                "include_raw": {"type": "boolean",
                                "description": "把 raw/ 原文层（转录稿/推文存档）纳入检索，默认 false。"
                                               "需要原话、具体细节、案例原文时才开；日常问答用摘要层更精准。"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch",
        "description": (
            "按 id 取单篇文档（Markdown 正文 + 白名单 frontmatter）。"
            "mode='outline' 只回「标题 + 每节首句」（约 500 字），先判相关性再取全文；max_chars 可截断。"
            "id 来自 search 结果，形如 concepts/精准定位 或 summaries/内容创作 & IP/xxx。"
            "取原始转录稿/原文时传 id='raw/子目录/文件名' 且 include_raw=true。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "页面 id 或 raw/ 相对路径"},
                "mode": {"type": "string", "enum": ["full", "outline"],
                         "description": "full=全文（默认）；outline=标题+每节首句，省 token"},
                "max_chars": {"type": "integer", "description": "正文最大字符数，0 或不传=全文"},
                "include_raw": {"type": "boolean", "description": "允许读取 raw/ 原文，默认 false"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "list_concepts",
        "description": (
            "列出全部概念页（约 60 个）及一句话定义，可按 category 过滤。"
            "先调用它可以拿到知识库的全局地图（约 2.4 万 token），再按需 search/fetch 深挖。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"category": {"type": "string", "description": "按分类过滤，可选"}},
        },
    },
    {
        "name": "stats",
        "description": "知识库概况：页面数、索引文档数、各目录页数、scope 说明、索引健康提示。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ask",
        "description": (
            "知识库问答增强版：先检索再判断问题复杂度（simple/complex/comparative）、"
            "覆盖度（0-1）与知识缺口（gap_topics）。适合回答『库里有相关内容吗、够不够』。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户问题"},
                "scope": {"type": "string", "enum": ["personal", "team"]},
            },
            "required": ["query"],
        },
    },
]

HANDLERS = {
    "search": tool_search,
    "fetch": tool_fetch,
    "list_concepts": tool_list_concepts,
    "stats": tool_stats,
    "ask": tool_ask,
}


# ── JSON-RPC 传输层 ────────────────────────────────────────────────────
def _send(payload: dict) -> None:
    _PROTO_OUT.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def _result(msg_id, result):
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id, code: int, message: str):
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def handle(req: dict) -> None:
    method = req.get("method")
    msg_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
        _result(msg_id, {
            "protocolVersion": proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "本地 LLM Wiki 知识库。建议：先用 list_concepts 拿全局概念地图，"
                "再用 search 定位、fetch 读全文。回答需锚定文档内容，未收录就说未收录。"
            ),
        })
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return  # 通知无需响应

    if method == "ping":
        _result(msg_id, {})
        return

    if method == "tools/list":
        _result(msg_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            _error(msg_id, -32602, f"未知工具：{name}")
            return
        try:
            data = fn(args)
            _result(msg_id, {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "isError": False,
            })
        except Exception as exc:
            _result(msg_id, {
                "content": [{"type": "text",
                             "text": f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=2)}"}],
                "isError": True,
            })
        return

    if msg_id is not None:
        _error(msg_id, -32601, f"未实现的方法：{method}")


def main() -> int:
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            _error(None, -32700, f"JSON 解析失败：{exc}")
            continue
        if isinstance(req, list):  # 批量
            for r in req:
                handle(r)
        else:
            handle(req)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
