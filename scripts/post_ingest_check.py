#!/usr/bin/env python3
"""入库/编译收尾体检（静默版）。

设计原则：**正常时一个字都不输出**（exit 0），有问题才说话，且每条都带"下一步跑什么"。
这样它能挂进 #ingest 收尾和 6:00 定时任务，而不会变成每天刷屏的噪音。

用法（在 vault 根目录）：
    PYTHONPATH="" python3 scripts/post_ingest_check.py
    PYTHONPATH="" python3 scripts/post_ingest_check.py --json     # 机器可读
    PYTHONPATH="" python3 scripts/post_ingest_check.py --max 5    # 最多列几条

退出码恒为 0：cron 的 no_agent 模式会把非零退出当成"任务跑挂"，体检发现问题不等于任务失败。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HEALTH = REPO_ROOT / "scripts" / "health.py"

# 体检项 → 下一步动作（给 agent/人看的修复提示）
NEXT_STEP = {
    "zvec_freshness": "跑 embed.py --incremental（索引落后于文件）",
    "zvec_ghost": "跑 embed.py --rebuild（索引里有、磁盘上没了的幽灵条目）",
    "broken_link": "修双链：改成现有页面名，或把该行改为纯文本",
    "empty_stub": "补正文或删掉空壳页（空壳页不会被 embed 收录，也会让页数对不上）",
    "index_sync": "重新生成 wiki/index.md（目录与导航不一致）",
    "log_coverage": "在 wiki_log.md 补记这批页面",
    "category_routing": "脚本已尝试自修；仍有残留需人工确认分类",
    "entity_index": "跑 entity_index.py 重建实体索引",
    "orphan_page": "可接受（孤儿页不强补链）；若该入库，检查 Step 6 是否漏建双链",
}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run_health() -> dict:
    p = subprocess.run(
        [sys.executable, str(HEALTH), "--json"],
        capture_output=True, cwd=str(REPO_ROOT),
        env={"PYTHONPATH": "", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    text = _decode(p.stdout or b"") or _decode(p.stderr or b"")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"status": "error", "issues": [{
            "check": "health_script", "severity": "error",
            "message": f"health.py 输出无法解析（rc={p.returncode}）：{text.strip()[:300]}",
        }]}


def main() -> int:
    # Windows 控制台默认 GBK，emoji/中文会 UnicodeEncodeError（同其他脚本的既有修复）
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if _reconfigure:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="入库收尾体检（正常静默）")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    ap.add_argument("--max", type=int, default=6, help="最多列几条问题（默认 6）")
    args = ap.parse_args()

    report = run_health()
    status = report.get("status", "unknown")
    issues = [i for i in report.get("issues", [])
              if i.get("severity") in ("warning", "error")]

    if args.json:
        print(json.dumps({"status": status, "issues": issues}, ensure_ascii=False, indent=2))
        return 0

    if status == "ok" and not issues:
        return 0  # 静默：正常不打扰

    icon = "🔴" if status == "error" or any(i.get("severity") == "error" for i in issues) else "⚠️"
    pages = report.get("pages")
    head = f"{icon} 体检未通过（status={status}"
    head += f"，页面 {pages}）" if pages else "）"
    lines = [head, f"共 {len(issues)} 条待处理："]

    seen: set[str] = set()
    shown = 0
    for it in issues:
        chk = it.get("check", "?")
        if shown >= args.max:
            break
        if chk in seen and shown >= 3:  # 同类太多时只留前几条
            continue
        seen.add(chk)
        path = it.get("path") or it.get("target") or ""
        msg = it.get("message", "")[:150]
        lines.append(f"- [{chk}] {msg}" + (f"  （{path}）" if path else ""))
        shown += 1

    if len(issues) > shown:
        lines.append(f"… 另有 {len(issues) - shown} 条同类问题")

    hints = [NEXT_STEP[c] for c in dict.fromkeys(i.get("check") for i in issues) if c in NEXT_STEP]
    if hints:
        lines.append("下一步：")
        lines.extend(f"  · {h}" for h in hints[:4])

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
