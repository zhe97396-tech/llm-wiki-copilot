#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""烟雾测试：用官方 MCP SDK 客户端跨进程调用 integrations/mcp/wikimcp.py。

跑法（demo 数据，不需要自己的知识库）::

    # 客户端用任意带 mcp 包的 python；服务端用装了 zvec+fastembed 的解释器
    LLM_WIKI_DATA=demo \
    LLM_WIKI_PYTHON=/path/to/embed-venv/python \
    python smoke_client.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
SERVER_PY = os.environ.get("LLM_WIKI_PYTHON") or sys.executable
VAULT = os.environ.get("LLM_WIKI_DATA") or str(HERE.parents[1] / "vault")


async def main() -> int:
    env = dict(os.environ)
    env.update({"LLM_WIKI_DATA": VAULT, "PYTHONPATH": "", "PYTHONUTF8": "1"})
    params = StdioServerParameters(
        command=SERVER_PY, args=["-E", "-s", str(HERE / "wikimcp.py")], env=env
    )
    failures = 0
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as sess:
            init = await sess.initialize()
            print(f"server: {init.serverInfo.name} {init.serverInfo.version}")
            tools = [t.name for t in (await sess.list_tools()).tools]
            print(f"tools : {tools}")

            sample = ""
            for name, args, probe in (
                ("stats", {}, lambda d: f"pages={d.get('pages')} index_docs={d.get('index_docs')}"),
                ("list_concepts", {}, lambda d: f"concepts={d.get('count')}"),
            ):
                res = await sess.call_tool(name, args)
                payload = json.loads(res.content[0].text)
                failures += 0 if not res.isError else 1
                print(f"{name:<14} isError={res.isError} {probe(payload)}")
                if name == "list_concepts" and payload.get("concepts"):
                    sample = payload["concepts"][0]["name"]

            # 用库里确实存在的概念名检索，确保不是"0 命中也算通过"
            if not sample:
                print("search         跳过（库里没有 concepts，无法构造有效查询）")
            else:
                res = await sess.call_tool("search", {"query": sample, "top_k": 3})
                payload = json.loads(res.content[0].text)
                ok = (not res.isError) and payload.get("count", 0) > 0
                failures += 0 if ok else 1
                print(f"{'search':<14} isError={res.isError} count={payload.get('count')} "
                      f"query={sample!r} (期望 >0)")

            # fetch 负例：不存在的 id 必须报错而不是返回空
            res = await sess.call_tool("fetch", {"id": "concepts/__不存在__"})
            print(f"{'fetch(负例)':<14} isError={res.isError} (期望 True)")
            failures += 0 if res.isError else 1

    print("RESULT:", "PASS" if failures == 0 else f"FAIL({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
