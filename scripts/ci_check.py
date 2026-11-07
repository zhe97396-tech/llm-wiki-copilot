#!/usr/bin/env python3
"""CI / 本地自检：把"改完检索到底有没有坏"变成一条命令。

用法（在仓库根目录）：
    # 只做静态与结构检查（不需要 zvec / fastembed，秒级）
    PYTHONPATH="" python scripts/ci_check.py

    # 完整检查（需要已装 zvec + fastembed，且数据目录已建好索引）
    PYTHONPATH="" python scripts/ci_check.py --data /path/to/vault --bench-cases bench/cases.example.json

检查项：
  1. 全量语法编译（scripts/ + integrations/mcp/）
  2. 分块索引模块可导入；切块函数在合成页面上确实切出多块
  3. search 的融合能力存在，目录去重参数未被改回 2/0.5
  4. MCP 暴露 6 个工具且 related 已注册
  5. （有 --data 且已建索引时）分块索引新鲜度 = OK
  6. （有 --data 时）真实 MCP 协议往返：search 有结果、related 有兄弟页
  7. （有 --bench-cases 时）回归台 hit@3 不低于阈值（默认 0.5，防明显退化）

退出码：0 全过；1 有失败（失败项逐条打印，不用猜）。
"""

from __future__ import annotations

import argparse
import ast
import compileall
import io
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
MCP_FILE = REPO_ROOT / "integrations" / "mcp" / "wikimcp.py"

FAILS: list[str] = []
PASSES: list[str] = []


def ok(msg: str) -> None:
    PASSES.append(msg)
    print(f"  OK   {msg}")


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  FAIL {msg}")


def step(name: str) -> None:
    print(f"\n[{name}]")


# ── 1. 语法编译 ─────────────────────────────────────────────────────
def check_compile() -> None:
    step("语法编译")
    good = True
    for target in (SCRIPTS, REPO_ROOT / "integrations" / "mcp"):
        if not target.exists():
            continue
        if not compileall.compile_dir(str(target), quiet=1, force=True):
            good = False
    if good:
        ok("scripts/ 与 integrations/mcp/ 全部可编译")
    else:
        fail("存在语法错误（见上方 traceback）")


# ── 2. 分块索引模块与切块行为 ───────────────────────────────────────
def check_chunking() -> None:
    step("分块索引模块")
    sys.path.insert(0, str(SCRIPTS))
    try:
        import zvec_chunk_config as C
    except Exception as exc:  # noqa: BLE001
        fail(f"zvec_chunk_config 导入失败：{type(exc).__name__}: {exc}")
        return
    ok("zvec_chunk_config 可导入")

    sample = (
        "## 第一节\n" + "这是一段足够长的正文，用来验证切块逻辑会把它当作一个独立的小节处理。" * 4 + "\n"
        "## 第二节\n" + "这是另一段同样足够长的正文，措辞不同，用来验证切块会产出多块而不是一块。" * 4
    )
    chunks = C.split_page("示例页", sample)
    if len(chunks) >= 2:
        ok(f"split_page 在合成页面上切出 {len(chunks)} 块")
    else:
        fail(f"split_page 只切出 {len(chunks)} 块，切块逻辑可能失效")
    if all(" > " in text.splitlines()[0] for _h, text in chunks):
        ok("块文本带 `页面 > 小节` 上下文前缀")
    else:
        fail("块文本缺少上下文前缀（contextual retrieval 失效）")


# ── 3. search 融合能力与参数 ────────────────────────────────────────
def check_search_shape() -> None:
    step("search 结构")
    src = (SCRIPTS / "search.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for fn in ("_fuse_chunk_retrieval", "_chunk_page_scores", "zvec_search"):
        if fn in names:
            ok(f"{fn} 存在")
        else:
            fail(f"{fn} 缺失（分块融合被删除？）")

    vals = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id in ("DIVERSITY_MAX_SAME_DIR", "DIVERSITY_PENALTY"):
                try:
                    vals[tgt.id] = ast.literal_eval(node.value)
                except Exception:
                    pass
    if vals.get("DIVERSITY_MAX_SAME_DIR") == 4 and abs(float(vals.get("DIVERSITY_PENALTY", 0)) - 0.8) < 1e-9:
        ok("目录去重参数为 4 / 0.8（未被改回 2 / 0.5）")
    else:
        fail(f"目录去重参数异常：{vals}（期望 MAX=4, PENALTY=0.8）")


# ── 4. MCP 工具注册 ─────────────────────────────────────────────────
def check_mcp_shape() -> None:
    step("MCP 工具注册")
    src = MCP_FILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    tools: list[str] = []
    handlers: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name == "TOOLS":
                for elt in getattr(node.value, "elts", []):
                    for k, v in zip(getattr(elt, "keys", []), getattr(elt, "values", [])):
                        if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                            tools.append(str(v.value))
            if name == "HANDLERS":
                handlers = [k.value for k in getattr(node.value, "keys", []) if isinstance(k, ast.Constant)]
    expected = {"search", "fetch", "related", "list_concepts", "stats", "ask"}
    if expected.issubset(set(tools)):
        ok(f"MCP 暴露 {len(tools)} 个工具：{', '.join(tools)}")
    else:
        fail(f"MCP 工具缺失：{sorted(expected - set(tools))}（实际 {tools}）")
    if "related" in handlers:
        ok("related 已注册到 HANDLERS")
    else:
        fail("related 未注册到 HANDLERS")


# ── 5/6. 真实数据：索引新鲜度 + MCP 往返 ───────────────────────────
def check_health_chunks(data: Path) -> None:
    step("分块索引新鲜度")
    env = dict(os.environ, LLM_WIKI_DATA=str(data), PYTHONPATH="")
    p = subprocess.run([sys.executable, str(SCRIPTS / "health.py"), "--check", "zvec_chunks"],
                       capture_output=True, text=True, env=env)
    out = (p.stdout or "") + (p.stderr or "")
    if "分块索引新鲜" in out:
        ok("分块索引逐块哈希一致")
    elif "分块索引未建立" in out:
        fail("分块索引未建立（先跑 scripts/embed_chunks.py --rebuild）")
    else:
        fail(f"分块索引落后或检查异常：{out.strip().splitlines()[-1] if out.strip() else '(无输出)'}")


def mcp_roundtrip(data: Path) -> None:
    step("MCP 协议往返")
    env = dict(os.environ, LLM_WIKI_DATA=str(data), PYTHONPATH="")
    proc = subprocess.Popen([sys.executable, "-E", str(MCP_FILE)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env)
    try:
        def send(obj) -> None:
            assert proc.stdin
            proc.stdin.write((json.dumps(obj, ensure_ascii=False) + chr(10)).encode("utf-8"))
            proc.stdin.flush()

        def recv():
            assert proc.stdout
            return json.loads(proc.stdout.readline().decode("utf-8", "replace"))

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "ci", "version": "0"}}})
        recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in recv()["result"]["tools"]]
        if "related" in names:
            ok(f"tools/list 返回 {len(names)} 个工具")
        else:
            fail(f"tools/list 缺 related：{names}")

        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "search", "arguments": {"query": "记住学过的东西", "top_k": 2}}})
        res = recv()
        if res.get("error"):
            fail(f"search 调用报错：{res['error']}")
            return
        hits = json.loads(res["result"]["content"][0]["text"]).get("results", [])
        if hits:
            ok(f"search 返回 {len(hits)} 条，top1={hits[0]['id']}")
        else:
            fail("search 返回 0 条（索引可能为空）")
            return

        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "related", "arguments": {"id": hits[0]["id"], "limit": 5}}})
        res = recv()
        if res.get("error"):
            fail(f"related 调用报错：{res['error']}")
            return
        d = json.loads(res["result"]["content"][0]["text"])
        if d.get("count", 0) > 0:
            ok(f"related 返回 {d['count']} 个同簇页（种子实体 {len(d.get('seed', {}).get('entities', []))} 个）")
        else:
            fail(f"related 返回 0 条：{d.get('note', '')}")
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
        except Exception:
            pass


# ── 7. 回归台阈值 ──────────────────────────────────────────────────
def check_bench(cases: Path, data: Path, min_hit3: float) -> None:
    step("检索回归台")
    env = dict(os.environ, LLM_WIKI_DATA=str(data), PYTHONPATH="")
    p = subprocess.run([sys.executable, str(SCRIPTS / "bench_search.py"), "--cases", str(cases), "--json"],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        fail(f"bench 执行失败：{(p.stderr or '').strip()[:300]}")
        return
    try:
        r = json.loads(p.stdout)
    except Exception as exc:  # noqa: BLE001
        fail(f"bench 输出无法解析：{exc}")
        return
    n = r.get("n", 0) or 1
    hit3 = r.get("hit3", 0) / n
    print(f"  bench: n={n} hit@1={r.get('hit1',0)}/{n} hit@3={r.get('hit3',0)}/{n} MRR={r.get('mrr',0):.3f}")
    if hit3 >= min_hit3:
        ok(f"hit@3 {hit3:.0%} ≥ 阈值 {min_hit3:.0%}")
    else:
        fail(f"hit@3 {hit3:.0%} < 阈值 {min_hit3:.0%}（检索明显退化）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="知识库数据根目录（含 wiki/ 与 .llm-wiki/）；不给则只做静态检查")
    ap.add_argument("--bench-cases", help="问题集 JSON；不给则跳过回归台")
    ap.add_argument("--min-hit3", type=float, default=0.5, help="hit@3 下限，默认 0.5")
    args = ap.parse_args()

    print(f"LLM Wiki CI 自检 | repo={REPO_ROOT}")
    check_compile()
    check_chunking()
    check_search_shape()
    check_mcp_shape()

    if args.data:
        data = Path(args.data).resolve()
        if not (data / "wiki").exists():
            fail(f"--data 下没有 wiki/：{data}")
        else:
            check_health_chunks(data)
            mcp_roundtrip(data)
            if args.bench_cases:
                check_bench(Path(args.bench_cases), data, args.min_hit3)

    print(f"\n结果：{len(PASSES)} 通过 / {len(FAILS)} 失败")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
