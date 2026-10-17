#!/usr/bin/env python3
"""check_distilled.py — #start 带读前过滤：已带读/已沉淀的文章禁止重复带读

判据（三信号任一命中即视为"已带读/已沉淀"）：
  1. frontmatter status == "distilled"          （#复盘 沉淀完毕）
  2. frontmatter interaction_count >= 1          （曾带读/交互过）
  3. wiki/conversations/ 存在 "{标题}_Chat_*.md" （深度解读版物理产物，最强信号）

用法：
  python scripts/check_distilled.py                      # 列出全部，已沉淀/可带读分区
  python scripts/check_distilled.py --check "<标题或文件名>"  # 单篇检查，命中输出 JSON 并 exit 1
  python scripts/check_distilled.py --mark "<标题或文件名>"   # 打带读标记（interaction_count+1, last_interacted=今天），#start 确认开始带读时调用
  python scripts/check_distilled.py --json               # 全量 JSON（供流程/看板用）

输出约定：
  --check 命中已沉淀 → 打印 JSON {"title":..., "distilled": true, "reasons":[...], "chat_file":...}，exit 1
  --check 未命中     → 打印 JSON {"title":..., "distilled": false}，exit 0
  --mark 成功        → 打印 JSON {"title":..., "marked": true, "interaction_count": N, "last_interacted": "..."}，exit 0
  --mark 未找到      → exit 3
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import DATA_ROOT, WIKI_DIR  # noqa: E402

SUMMARIES_DIR = WIKI_DIR / "summaries"
CONVERSATIONS_DIR = WIKI_DIR / "conversations"


def read_frontmatter(content: str) -> dict:
    """解析 YAML frontmatter 关键字段（不引第三方 YAML 库，正则足够）。"""
    fields = {}
    if not content.startswith("---"):
        return fields
    end = content.find("---", 3)
    if end == -1:
        return fields
    fm = content[3:end]
    for line in fm.splitlines():
        m = re.match(r'^([A-Za-z_]+):\s*(.+)$', line.strip())
        if m:
            key, val = m.group(1), m.group(2).strip()
            val = val.strip('"\'')
            fields[key] = val
    return fields


def find_chat_file(summary_path: Path, title: str) -> Path | None:
    """在 conversations/ 下找 "{标题}_Chat_*.md"。

    文件名与 frontmatter title 可能不一致（全角冒号 vs 连字符等），
    优先用文件名 stem 匹配，title 作为兜底。
    """
    if not CONVERSATIONS_DIR.exists():
        return None
    candidates = [summary_path.stem, title]
    for stem in candidates:
        matches = list(CONVERSATIONS_DIR.glob(f"{stem}_Chat_*.md"))
        if matches:
            return matches[0]
    return None


def classify(summary_path: Path) -> dict:
    content = summary_path.read_text(encoding="utf-8")
    fm = read_frontmatter(content)
    title = fm.get("title") or summary_path.stem

    reasons = []
    chat_file = None

    if fm.get("status") == "distilled":
        reasons.append("status=distilled")
    try:
        if int(fm.get("interaction_count", 0)) >= 1:
            reasons.append(f"interaction_count={fm.get('interaction_count')}")
    except (TypeError, ValueError):
        pass

    chat_file = find_chat_file(summary_path, title)
    if chat_file:
        reasons.append("conversations/ 有深度解读版")

    return {
        "path": str(summary_path.relative_to(DATA_ROOT)).replace("\\", "/"),
        "title": title,
        "status": fm.get("status", ""),
        "interaction_count": fm.get("interaction_count", "0"),
        "last_interacted": fm.get("last_interacted", ""),
        "distilled": bool(reasons),
        "reasons": reasons,
        "chat_file": str(chat_file.relative_to(DATA_ROOT)).replace("\\", "/") if chat_file else None,
    }


def find_summary(query: str, results: list[dict]) -> dict | None:
    """按标题/文件名模糊匹配一篇总结文档。"""
    q = query.strip().replace(".md", "")
    for r in results:
        if q in (r["title"], r["path"]):
            return r
    for r in results:
        if r["title"].startswith(q) or Path(r["path"]).stem.startswith(q):
            return r
    for r in results:
        if q in r["title"]:
            return r
    return None


def mark_distilled(summary_path: Path) -> dict:
    """打带读标记：interaction_count+1，last_interacted=今天。

    幂等：已是 distilled 状态的不重复加标记（直接跳过，防误伤）。
    """
    from datetime import date

    content = summary_path.read_text(encoding="utf-8")
    fm = read_frontmatter(content)
    title = fm.get("title") or summary_path.stem

    if fm.get("status") == "distilled":
        return {"title": title, "marked": False, "reason": "already distilled"}

    today = date.today().isoformat()
    try:
        count = int(fm.get("interaction_count", "0")) + 1
    except (TypeError, ValueError):
        count = 1

    # 只替换 frontmatter 内的字段（文件前 4 行区域）
    new_content = content
    if "interaction_count:" in new_content.split("---", 2)[1] if new_content.startswith("---") else False:
        new_content = re.sub(
            r'^(interaction_count):\s*.*$',
            f'interaction_count: {count}',
            new_content,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        # 无该字段则插入到 frontmatter 结束前
        end = new_content.find("---", 3)
        new_content = new_content[:end] + f'interaction_count: {count}\n' + new_content[end:]

    if "last_interacted:" in (new_content.split("---", 2)[1] if new_content.startswith("---") else ""):
        new_content = re.sub(
            r'^(last_interacted):\s*.*$',
            f'last_interacted: "{today}"',
            new_content,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        end = new_content.find("---", 3)
        new_content = new_content[:end] + f'last_interacted: "{today}"\n' + new_content[end:]

    summary_path.write_text(new_content, encoding="utf-8")
    return {"title": title, "marked": True, "interaction_count": count, "last_interacted": today}


def main() -> int:
    ap = argparse.ArgumentParser(description="已带读/已沉淀文章过滤")
    ap.add_argument("--check", help="单篇检查：标题或文件名（支持模糊匹配）")
    ap.add_argument("--mark", help="打带读标记：interaction_count+1, last_interacted=今天")
    ap.add_argument("--json", action="store_true", help="全量输出 JSON")
    args = ap.parse_args()

    if not SUMMARIES_DIR.exists():
        print(json.dumps({"error": f"summaries 目录不存在: {SUMMARIES_DIR}"}, ensure_ascii=False))
        return 2

    summaries = sorted(SUMMARIES_DIR.rglob("*.md"))
    results = [classify(p) for p in summaries]

    if args.check:
        query = args.check.strip().replace(".md", "")
        hit = find_summary(query, results)
        if hit is None:
            print(json.dumps({"query": args.check, "distilled": False, "error": "未找到匹配的总结文档"}, ensure_ascii=False))
            return 3
        print(json.dumps(hit, ensure_ascii=False, indent=2))
        return 1 if hit["distilled"] else 0

    if args.mark:
        hit = find_summary(args.mark, results)
        if hit is None:
            print(json.dumps({"query": args.mark, "marked": False, "error": "未找到匹配的总结文档"}, ensure_ascii=False))
            return 3
        summary_path = DATA_ROOT / hit["path"]
        result = mark_distilled(summary_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    distilled = [r for r in results if r["distilled"]]
    fresh = [r for r in results if not r["distilled"]]

    if args.json:
        print(json.dumps({"total": len(results), "distilled": distilled, "fresh": fresh}, ensure_ascii=False, indent=2))
        return 0

    print(f"📊 总结文档共 {len(results)} 篇 | 已带读/已沉淀 {len(distilled)} 篇 | 可带读 {len(fresh)} 篇\n")
    print("🚫 已带读/已沉淀（#start 应过滤，不重复带读）：")
    for r in distilled:
        flag = "💎" if r["status"] == "distilled" else "📖"
        chat = f" → {r['chat_file']}" if r["chat_file"] else ""
        print(f"  {flag} {r['title']}  [{', '.join(r['reasons'])}]{chat}")
    print("\n✅ 可带读（未沉淀）：")
    for r in fresh:
        print(f"  📄 {r['title']}  ({r['path']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
