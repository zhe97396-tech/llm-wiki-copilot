#!/usr/bin/env python3
"""Ask 覆盖度回归台：coverage 说"库里有/没有"，到底准不准。

和 bench_search.py 的分工：
  - bench_search.py 测**找得准不准**（hit@1 / hit@3 / MRR）
  - bench_ask.py    测**够不够答**（coverage 对"有料/无料"的判定）

用法：
    PYTHONPATH="" python scripts/bench_ask.py --cases bench/ask_cases.example.json
    PYTHONPATH="" python scripts/bench_ask.py --cases my_ask_cases.json --max-false-alarm 0 --json

问题集 JSON 格式：
    {"positives": ["库里确实有落点的问题", ...],
     "negatives": ["库里明确没有的主题", ...]}

判据：
  - 负例 → 应判 insufficient；判成 complete/partial 都算**误报**（最痛的一种：
    Agent 会拿不相关页面硬凑答案，而且错得没有信号）
  - 正例 → 应判 complete 或 partial；判 insufficient 算**漏报**
  - 二分类准确率 = (负例判没料 + 正例判有料) / 总数

加题纪律：别为了让数字好看删题。负例必须是"库里真的没有"——加之前先跑
search 看 top3 标题，沾边就别当负例。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ask import ask  # noqa: E402

WITH_CONTENT = ("complete", "partial")


def run(queries: list[str], verbose: bool) -> list[dict]:
    rows = []
    for q in queries:
        r = ask(q)
        row = {
            "q": q,
            "coverage": r["coverage"],
            "level": r["coverage_level"],
            "basis": r.get("coverage_basis"),
            "gaps": r["gap_topics"],
            "top1": (r["results"][0]["title"][:34] if r["results"] else ""),
        }
        rows.append(row)
        if verbose:
            mark = "✓" if row["level"] in WITH_CONTENT else "✗"
            print(f"  {mark} cov={row['coverage']:.2f} {row['level']:12s} "
                  f"basis={str(row['basis']):13s} | {q}")
    return rows


def summarize(rows: list[dict], want_content: bool) -> dict:
    n = len(rows) or 1
    complete = sum(1 for r in rows if r["level"] == "complete")
    partial = sum(1 for r in rows if r["level"] == "partial")
    insuff = sum(1 for r in rows if r["level"] == "insufficient")
    right = (complete + partial) if want_content else insuff
    return {
        "n": len(rows), "complete": complete, "partial": partial, "insufficient": insuff,
        "correct": right, "wrong": len(rows) - right, "accuracy": round(right / n, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Ask coverage 回归台")
    ap.add_argument("--cases", required=True, help="问题集 JSON（positives / negatives）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="不逐条打印")
    ap.add_argument("--max-false-alarm", type=int, default=None,
                    help="允许的最大误报数；超过则 exit 1（给 CI 用）")
    args = ap.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    positives = cases.get("positives") or []
    negatives = cases.get("negatives") or []
    if not positives and not negatives:
        print(f"问题集为空：{args.cases}", file=sys.stderr)
        return 1

    verbose = not (args.json or args.quiet)
    if not args.json:
        print(f"正例 {len(positives)} 条（应判有料）/ 负例 {len(negatives)} 条（应判没料）")
        print("\n=== 正例 ===")
    pos_rows = run(positives, verbose)
    if not args.json:
        print("\n=== 负例 ===")
    neg_rows = run(negatives, verbose)

    ps = summarize(pos_rows, want_content=True)
    ns = summarize(neg_rows, want_content=False)
    total = ps["n"] + ns["n"]
    acc = (ps["correct"] + ns["correct"]) / total if total else 0.0

    result = {
        "positives": ps,
        "negatives": ns,
        "binary_accuracy": round(acc, 3),
        "false_alarm": ns["wrong"],   # 库里没有却说有（最痛）
        "false_miss": ps["wrong"],    # 库里有却说没有
        "worst_negative": max(neg_rows, key=lambda r: r["coverage"], default=None),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if positives:
            print(f"\n--- 正例 ---\n  complete {ps['complete']} / partial {ps['partial']} / insufficient {ps['insufficient']}"
                  f"  → 判有料 {ps['correct']}/{ps['n']}")
        if negatives:
            print(f"--- 负例 ---\n  complete {ns['complete']} / partial {ns['partial']} / insufficient {ns['insufficient']}"
                  f"  → 判没料 {ns['correct']}/{ns['n']}")
        print(f"\n二分类准确率 {acc:.0%}    误报(无料说有) {ns['wrong']}    漏报(有料说无) {ps['wrong']}")
        if result["worst_negative"]:
            w = result["worst_negative"]
            print(f"最高分的负例：{w['coverage']:.2f} {w['level']} | {w['q']}")

    if args.max_false_alarm is not None and ns["wrong"] > args.max_false_alarm:
        print(f"\n✗ 误报 {ns['wrong']} > 允许 {args.max_false_alarm}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
