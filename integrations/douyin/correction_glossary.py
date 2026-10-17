#!/usr/bin/env python3
"""确定性规则纠错词表 —— LLM 校对的「兜底层」（A1，2026-08-20 蒸馏落地）。

来源：
- Backtthefuture/video-transcript 的 podcast_glossary.py（规则替换思路）
# 常见错误模式来源：漏校池规则表（本项目积累）+ 视频转录常见谐音/同音错误

定位：在 LLM 校对之前做一层确定性子串替换，把已确认的高频 ASR 误识别
零成本修掉，LLM 只处理词表外的未知错误 → 更准、更省 token、更快。

收录标准（严格，宁缺毋滥）：
1. 只收「发音相近、替换后语法通顺但语义错误」的 ASR 误识别
2. 高辨识度：错误词几乎不可能是正常表达（否则不加）
3. 纯 ASCII 按词边界匹配（“infa”不命中“infant”）
4. 常用词 / 同音合法词一律不收（见下方 EXCLUDED 说明）

匹配规则：
- 按「错误词长度」降序匹配，先长后短，避免短词误伤
- 纯 ASCII 用词边界 (?<![A-Za-z0-9])pat(?![A-Za-z0-9])
- 中文短语直接子串替换
- 匹配默认区分大小写（避免误伤合法的大小写变体）

扩展方式：
- 同目录 correction_glossary_extra.json = [["错误词","修正词"], ...]
  → 自动加载并优先于内置词表（用于具体领域 / 节目专属词）
- 新增错误：先加进 extra；确认通用且安全后移入 DEFAULT

⚠️ 明确不收录（易误伤，留给 LLM 上下文判断）：
- 同归于尽→同归于寂： “同归于尽”本身是合法常用词组，不能盲替，需古文上下文
- 积极了→廉价： “积极了”可是正常表达（“变得积极了”）
- kimi→Kimi： 可能是人名/其他命名
- RG/IG→RAG： 太短且可能是合法缩写
- nextS→Next.js： 可能是别的“next s”
- 长征任务→长程任务： “长征任务”可合法
- 应往上挤→硬往上挤： “应往上挤”可合法
"""

import json
import os
import re

# (错误词, 修正词) —— 只放高置信、高辨识度的 ASR 误识别
DEFAULT_GLOSSARY: list[tuple[str, str]] = [
    # ── 技术术语（英文误识别，多为 ASR 小写化/拼错）──
    ("deep sik", "DeepSeek"),
    ("deep sick", "DeepSeek"),
    ("dev seek", "DeepSeek"),
    ("dev sig", "DeepSeek"),
    ("deep sig", "DeepSeek"),
    ("DeepSeekrone", "DeepSeek R1"),
    ("DeepSeekR1", "DeepSeek R1"),
    ("mar skillss", "mattpocock skills"),
    ("GBT 四 o", "GPT-4o"),
    ("GBT 四 O", "GPT-4o"),
    ("GBT4o", "GPT-4o"),
    ("GBT 三点五", "GPT-3.5"),
    ("GBT 3.5", "GPT-3.5"),
    ("GBT", "GPT"),
    ("open claw", "OpenClaw"),
    ("Open Claw", "OpenClaw"),
    ("cloud code", "Claude Code"),
    ("Cloud Code", "Claude Code"),
    ("invadding", "embedding"),
    ("graphraph", "Graph"),
    ("狼buff", "LangGraph"),
    ("gettla", "GitLab"),
    ("A镜体个", "Agentic"),
    ("A进题", "Agentic"),
    ("cloudMD", "claude.md"),
    ("colMD", "claude.md"),
    ("agMD", "agents.md"),
    ("contexMD", "context.md"),
    ("benmark", "Benchmark"),
    ("办曲", "Benchmark"),
    ("办企", "Benchmark"),
    ("半形", "Benchmark"),
    ("band 曲", "Benchmark"),
    ("batch 曲", "Benchmark"),
    ("mimax", "MiniMax"),
    ("MIMXX", "MiniMax"),
    # ── 技术/中文术语误识别 ──
    ("自制制能体", "自治智能体"),
    ("训谋型", "训模型"),
    ("核成数据", "合成数据"),
    ("博客媒体", "播客媒体"),
    ("床投", "创投"),
    ("巨身", "具身"),
    ("模婴厂", "模型厂"),
    ("混员", "混元"),
    ("梭嗨", "梭哈"),
    # ── 非技术中文误识别（高辨识度）──
    ("射牛", "社牛"),
    ("拮局", "拮据"),
    ("负贷", "负债"),
    ("林宗一演", "临终遗言"),
    ("林踪一言", "临终遗言"),
    ("搁肢", "搁置"),
]

_ASCII_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .+\-]*")


def _pattern_for(wrong: str) -> "re.Pattern[str]":
    """构造匹配 pattern：纯 ASCII 词加词边界；中文按子串直配。"""
    body = re.escape(wrong)
    if _ASCII_RE.fullmatch(wrong):
        body = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"
    return re.compile(body)


# 按错误词长度降序，先长后短（避免短词先命中误伤）
_PATTERNS = sorted(
    [(wrong, right) for wrong, right in DEFAULT_GLOSSARY],
    key=lambda pr: len(pr[0]),
    reverse=True,
)
_COMPILED = [(re.compile(_pattern_for(wrong)), right) for wrong, right in _PATTERNS]


def _load_extra() -> list[tuple[str, str]]:
    """加载同目录 correction_glossary_extra.json（若有），优先于内置词表。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "correction_glossary_extra.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [(str(p[0]), str(p[1])) for p in data if isinstance(p, (list, tuple)) and len(p) >= 2]
    except Exception:
        return []


def _compiled_rules() -> list:
    extra = _load_extra()
    if not extra:
        return _COMPILED
    extra_sorted = sorted(extra, key=lambda pr: len(pr[0]), reverse=True)
    # 内置 + 附加（附加在前，优先命中）
    base = sorted(DEFAULT_GLOSSARY, key=lambda pr: len(pr[0]), reverse=True)
    rules = [(re.compile(_pattern_for(w)), ok) for w, ok in extra_sorted] + [
        (re.compile(_pattern_for(w)), ok) for w, ok in base
    ]
    return rules


def apply_glossary(text: str) -> tuple[str, list[tuple[str, str]]]:
    """对文本应用确定性纠错词表，返回 (修正后文本, 命中的 [(原, 新), ...])。

    - 只做确定性子串替换，不改变语序
    - 不中断、不抛错：异常时返回原文并记录
    """
    if not text:
        return text, []
    corrected = text
    fixes: list[tuple[str, str]] = []
    for pattern, right in _compiled_rules():
        m = pattern.search(corrected)
        if m:
            corrected = pattern.sub(lambda _: right, corrected)
            fixes.append((m.group(0), right))
    return corrected, fixes


if __name__ == "__main__":
    # 自测：跑一遍，检查匹配是否符合预期
    import sys

    samples = [
        "今天讲 RAG 和 invadding，还有 A镜体个 的 cloudMD",
        "社牛 是社交能力强的俗称，别混成拮局",
        "deep sik 发布了新的 GBT 模型",
        "这是 normal 表达和 infa 词边界的测试",
        "同归于尽 这种古文引用不要乱替",
    ]
    for s in samples:
        out, fixes = apply_glossary(s)
        print(f"IN : {s}")
        print(f"OUT: {out}")
        print(f"FIX: {fixes}")
        print()
