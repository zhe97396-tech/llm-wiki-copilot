#!/usr/bin/env python3
"""补跑 LLM 校对：对已生成但未成功校对的抖音 md 文件重新校对。

用法: python recheck_correct.py "<md路径>"
"""
import importlib.util
import re
import sys
from pathlib import Path

# 与本脚本同目录（开源版不再依赖固定安装路径）
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

spec = importlib.util.spec_from_file_location(
    "douyin_transcribe", str(_HERE / "douyin_transcribe.py")
)
dt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dt)


def main():
    md_path = sys.argv[1]
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 解析 frontmatter
    fm_match = re.match(r"^---\n(.*?)\n---\n", content, re.S)
    if not fm_match:
        print("✗ 无 frontmatter，跳过", file=sys.stderr)
        return 1
    fm = fm_match.group(1)
    title_m = re.search(r"^title: (.*)$", fm, re.M)
    tags_m = re.search(r"^tags: \[(.*)\]$", fm, re.M)
    title = title_m.group(1).strip() if title_m else ""
    tags = [t.strip() for t in tags_m.group(1).split(",")] if tags_m else []

    # 提取逐字稿
    body = content[fm_match.end():]
    sec_m = re.search(r"## 逐字稿\n\n(.*)$", body, re.S)
    if not sec_m:
        print("✗ 未找到 ## 逐字稿 段落", file=sys.stderr)
        return 1
    transcript = sec_m.group(1).strip()

    # 校对
    corrected_text = dt.llm_correct_and_segment(transcript, title, tags)
    if corrected_text == transcript or not corrected_text.strip():
        print("✗ 校对失败或返回原文，文件未修改", file=sys.stderr)
        return 1

    # 替换逐字稿 + 更新状态标记
    new_body = body[:sec_m.start()] + "## 逐字稿\n\n" + corrected_text.strip() + "\n"
    new_content = content[:fm_match.end()] + new_body
    new_content = new_content.replace("llm_corrected: false", "llm_corrected: true")
    new_content = new_content.replace("LLM校对：否", "LLM校对：是")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"✓ 校对完成并写回: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
