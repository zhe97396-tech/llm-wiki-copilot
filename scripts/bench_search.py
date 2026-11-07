#!/usr/bin/env python3
"""检索回归台：改检索前必跑，别凭感觉说"变好了"。

用法：
    # 1) 准备问题集（放你自己的真实提问，一条一个问题 + 期望命中的页面 id）
    cp bench/cases.example.json my-cases.json
    # 2) 跑
    PYTHONPATH="" python scripts/bench_search.py --cases my-cases.json
    PYTHONPATH="" python scripts/bench_search.py --cases my-cases.json --json
    PYTHONPATH="" python scripts/bench_search.py --cases my-cases.json --verbose

问题集格式（JSON 数组）：
    [{"q": "口语化的真实提问", "expect": ["concepts/某概念", "summaries/某分类/某页"]}]

指标：
    hit@1 / hit@3 / MRR@10 —— expect 中任一页出现在前 N 位即算命中。
    建议基线至少 15-20 条，否则噪声比信号大。

为什么要自己的问题集：用页面标题去搜自己只能得到 100% 的假成绩。
真实提问是口语、带场景、缺关键词的，那才是检索要面对的输入。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import search as S  # noqa: E402


def load_cases(path: Path) -> list[tuple[str, list[str]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for row in data:
        q = (row.get("q") or "").strip()
        expect = row.get("expect") or []
        if isinstance(expect, str):
            expect = [expect]
        if q and expect:
            cases.append((q, list(expect)))
    return cases


def run(cases: list[tuple[str, list[str]]], top_k: int = 10) -> dict:
    h1 = h3 = 0
    mrr = 0.0
    rows = []
    for q, expected in cases:
        try:
            res = S.search(q, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            rows.append({"q": q, "error": f"{type(exc).__name__}: {exc}"})
            continue
        ids = [r.get("page_id") for r in res]
        rank = next((i for i, pid in enumerate(ids, 1) if pid in expected), None)
        h1 += rank == 1
        h3 += bool(rank and rank <= 3)
        mrr += (1 / rank) if rank else 0.0
        rows.append({"q": q, "rank": rank, "top1": ids[0] if ids else None, "top3": ids[:3],
                     "chunk_hit": sum(1 for r in res if r.get("chunk_hit"))})
    n = len(cases)
    return {"n": n, "hit1": h1, "hit3": h3, "mrr": (mrr / n) if n else 0.0, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, help="问题集 JSON 路径")
    ap.add_argument("--top", type=int, default=10, help="取前 N 条判命中，默认 10")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="逐条打印排名与 top1")
    args = ap.parse_args()

    path = Path(args.cases)
    if not path.exists():
        print(f"问题集不存在：{path}\n先 cp bench/cases.example.json my-cases.json 再填自己的提问", file=sys.stderr)
        return 2
    cases = load_cases(path)
    if not cases:
        print("问题集为空（每条需要 q 和 expect）", file=sys.stderr)
        return 2

    r = run(cases, top_k=args.top)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0

    mode = "chunk ON" if os.environ.get("LLM_WIKI_CHUNK_SEARCH", "").lower() not in ("0", "off", "false", "no") else "chunk OFF"
    n = r["n"]
    print(f"分块融合：{mode}")
    print(f"queries={n}")
    print(f"hit@1  = {r['hit1']}/{n}  ({r['hit1']/n:.0%})")
    print(f"hit@3  = {r['hit3']}/{n}  ({r['hit3']/n:.0%})")
    print(f"MRR@10 = {r['mrr']:.3f}")
    if args.verbose:
        print("\n逐条：")
        for row in r["rows"]:
            print(f"  rank={row.get('rank')} chunk={row.get('chunk_hit', 0)} | {row['q']} → {row.get('top1')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
