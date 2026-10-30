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
import sys
import traceback
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


def _scope_filter(results: list[dict], scope: str) -> list[dict]:
    """team 范围再做一次路径白名单兜底（类型过滤之外的双保险）。"""
    if scope != "team":
        return results
    return [r for r in results if str(r.get("path", "")).startswith("wiki/")
            and any(r["path"].split("wiki/", 1)[-1].startswith(p) for p in TEAM_PREFIXES)]


def _exclude_types(scope: str) -> set[str] | None:
    """scope 层面的硬隔离（team 只出知识本体）。

    raw/ 与 github-stars/ 的默认排除已下沉到 search() 内部（include_raw / include_stars）。
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
                       include_stars=bool(args.get("include_stars")),
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


def tool_fetch(args: dict) -> dict:
    pid = (args.get("id") or "").strip().lstrip("/")
    if not pid:
        raise ValueError("id 不能为空")
    include_raw = bool(args.get("include_raw"))

    # 1) 索引内页面（concepts/xxx、summaries/分类/xxx …）
    mods = _mods()
    path = mods["pages"].get(pid.replace(".md", ""))

    # 2) 允许显式取 raw/（未进索引，仅供追溯原文）
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
    return {
        "id": pid,
        "path": str(path.relative_to(VAULT)).replace("\\", "/"),
        "frontmatter": {k: fm[k] for k in keep if k in fm},
        "body": text,
        "chars": len(text),
    }


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
    mods = _mods()
    pages = mods["pages"]
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

    return {
        "vault": str(VAULT),
        "pages": len(pages),
        "index_docs": index_docs,
        "by_dir": dict(sorted(by_dir.items(), key=lambda kv: -kv[1])),
        "scopes": {
            "personal": "全库（与 Hermes 侧一致，含 topics / github-stars / conversations）",
            "team": "仅 wiki/concepts + wiki/summaries + wiki/evergreen",
        },
        "note": "raw/ 未进索引：搜不到原文，可用 fetch(id='raw/xxx', include_raw=true) 按路径取。",
    }


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
            "include_raw=true 才检索 raw/ 原文层（默认关）；include_stars=true 才含 github-stars/"
            "（默认关，查收藏项目用 ghstars）。"
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
                "include_stars": {"type": "boolean",
                                  "description": "把 github-stars/ 收藏仓库纳入检索，默认 false。"
                                                 "知识本体与收藏仓库物理隔离：查 star 过的项目请用 ghstars 的 stars_search。"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch",
        "description": (
            "按 id 取单篇文档全文（Markdown 正文 + 白名单 frontmatter）。"
            "id 来自 search 结果，形如 concepts/精准定位 或 summaries/内容创作 & IP/xxx。"
            "取原始转录稿/原文时传 id='raw/子目录/文件名' 且 include_raw=true。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "页面 id 或 raw/ 相对路径"},
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
