#!/usr/bin/env python3
"""
抖音批量转录（方案B：模型一次加载、N 条复用；下载→转录→删临时→下一条）

设计目标：不动单链接脚本 douyin_transcribe.py；N 条视频共享一次 SenseVoice 模型加载，
省掉每条 10-15s 的模型加载开销，同时不做常驻 daemon（模型只在跑批那几分钟占 ~600MB，
用完释放，不 24 小时挂内存）。

兼容两种输入：
  - 单条链接：直接 `python douyin_batch.py "<URL>"`
  - 多条链接：`python douyin_batch.py "<URL1>" "<URL2>" ...` 或多个链接放一个文本文件（每行一条）用 `--file`
  - 飞书多维表格：飞书收集管道把「收集记录」表里待处理的抖音链接集到一个 .txt 文件，用 `--file` 喂进来

去重/续跑：每条先查账本（.llm-wiki/douyin_ledger.db 按 video_id），已转录的自动跳过；
跑批中断后重跑，已记账的自动跳过 = 可续传。

每条完整链路仍保持与单链接一致：A1 词表 → A2(patch 默认，失败回退全文校对) → markdown → 原子写入+账本。

用法：
  python douyin_batch.py "<URL1>" "<URL2>"
  python douyin_batch.py --file urls.txt --output-dir ".../00-INBOX"
  python douyin_batch.py --file feishu_pending_douyin.txt   # 飞书管道喂入
"""

import argparse
from datetime import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

# -I 隔离模式下脚本目录不在 sys.path，需显式加入才能 import 同目录的 douyin_transcribe
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from douyin_transcribe import (
    ASR_PYTHON,
    DEFAULT_LEDGER_DIR,
    DEFAULT_OUTPUT_DIR,
    LLM_CORRECT_ENABLED,
    apply_glossary,
    apply_patch,
    clean_text,
    content_sha256,
    download_and_extract_audio,
    extract_video_id,
    fetch_video_meta,
    generate_markdown,
    ledger_check,
    ledger_write,
    llm_correct_and_segment,
    llm_patch_and_segment,
    preorganize_segments,
    atomic_write,
)


# ============================================================
# 链接收集
# ============================================================

def collect_links(args) -> list:
    """从 CLI 参数 / --file / stdin 收集去重后的链接列表。"""
    links = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    links.append(line)
    if not links and not sys.stdin.isatty():
        for line in sys.stdin:
            line = line.strip()
            if line and not line.startswith("#"):
                links.append(line)
    # 去重（保持顺序）
    seen, uniq = set(), []
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


# ============================================================
# 单模型批量转录（-I 子进程，加载一次循环转全部）
# ============================================================

def transcribe_batch_audio(wav_map: dict, work_dir: str, timeout: int = 5400) -> dict:
    """一次启动 SenseVoice，循环转录多段音频。wav_map = {video_id: wav_path}

    结果写临时 JSON 返回 {video_id: text}。加载一次模型，N 段复用。
    """
    jobs_file = os.path.join(work_dir, "asr_jobs.json")
    result_file = os.path.join(work_dir, "asr_batch_result.json")
    with open(jobs_file, "w", encoding="utf-8") as f:
        json.dump(wav_map, f, ensure_ascii=False)

    script = '''import sys, json, os
os.environ['TQDM_DISABLE'] = '1'
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess
# 本地缓存优先（2026-08-29 修复：modelscope 目录树接口服务端故障导致 hub ID 版本校验必失败；
# 缓存缺失时仍回退 hub 下载。备份：douyin_batch.py.bak_20260829）
_ms_hub = os.path.join(os.path.expanduser('~'), '.cache', 'modelscope', 'hub', 'iic')
_sv = os.path.join(_ms_hub, 'SenseVoiceSmall')
_vad = os.path.join(_ms_hub, 'speech_fsmn_vad_zh-cn-16k-common-pytorch')
model = AutoModel(
    model=_sv if os.path.isdir(_sv) else 'iic/SenseVoiceSmall',
    vad_model=_vad if os.path.isdir(_vad) else 'fsmn-vad',
    vad_kwargs={'max_single_segment_time': 30000},
    device='cpu',
    disable_update=True,
)
jobs = json.load(open(stdin_jobs, encoding='utf-8'))
out = {}
for vid, wav in jobs.items():
    try:
        res = model.generate(input=wav, language='zh', use_itn=True, batch_size_s=60)
        text = ''
        for r in (res or []):
            if 'text' in r:
                text += rich_transcription_postprocess(r['text']) + '\\n\\n'
        out[vid] = text.strip()
    except Exception as e:
        out[vid] = '__ASR_ERROR__: ' + str(e)[:200]
json.dump(out, open(stdin_result, 'w', encoding='utf-8'), ensure_ascii=False)
'''
    # 用占位符替换，避免与嵌入式代码里的 %s / % 冲突
    script = script.replace("stdin_jobs", repr(jobs_file)).replace("stdin_result", repr(result_file))

    r = subprocess.run([ASR_PYTHON, "-I", "-c", script], timeout=timeout)
    if not os.path.exists(result_file):
        raise RuntimeError(f"批量转录结果文件未生成 (returncode={r.returncode})")
    with open(result_file, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# 单视频整理（复用 douyin_transcribe 的逻辑）
# ============================================================

def organize_text(meta: dict, cleared_text: str, full_correct: bool):
    """对单个转录文本跑 A1 词表 + A2(patch默认)或全文校对。返回 (final_text, corrected)。"""
    glossed, gloss_fixes = apply_glossary(cleared_text)
    corrected = False
    final = glossed
    if LLM_CORRECT_ENABLED:
        use_patch = not full_correct
        if use_patch:
            blocks = preorganize_segments(glossed)
            patch = llm_patch_and_segment(blocks, meta["title"], meta["tags"]) if blocks else None
            if patch is not None:
                final = apply_patch(blocks, patch)
                corrected = final != glossed
            else:
                final = llm_correct_and_segment(glossed, meta["title"], meta["tags"])
                corrected = final != glossed
        else:
            final = llm_correct_and_segment(glossed, meta["title"], meta["tags"])
            corrected = final != glossed
    return final, corrected, len(gloss_fixes)


def _persist_summary(ledger_dir, summary: dict):
    """把批量运行汇总写到账本目录下一份 JSON（覆盖）+ 追加一行日志。

    监控脚本/一次性 cron 据此验证「批量路径是否真的跑了 + 结果如何」。
    """
    os.makedirs(ledger_dir, exist_ok=True)
    last_path = os.path.join(ledger_dir, "douyin_batch_last.json")
    atomic_write(last_path, json.dumps(summary, ensure_ascii=False, indent=2))
    log_path = os.path.join(ledger_dir, "douyin_batch.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{summary['ts']} | total={summary['total']} "
                    f"ok={len(summary['ok'])} failed={len(summary['failed'])} "
                    f"skipped={len(summary['skipped'])}\n")
    except Exception:
        pass


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="抖音批量转录（模型一次加载、N条复用）")
    parser.add_argument("urls", nargs="*", help="抖音分享链接（可多个；或用 --file 指向每行一条的文本文件）")
    parser.add_argument("--file", help="包含多条抖音链接的文本文件（每行一条，# 开头为注释）")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="输出目录（默认00-INBOX）")
    parser.add_argument("--ledger-dir", default=DEFAULT_LEDGER_DIR, help="防重账本目录")
    parser.add_argument("--full-correct", action="store_true", help="强制旧全文校对（默认走 A2 patch 式，失败自动回退）")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时文件（调试用）")
    args = parser.parse_args()

    links = collect_links(args)
    if not links:
        print("没有输入任何链接", file=sys.stderr)
        return 2
    print("=" * 50, file=sys.stderr)
    print(f"抖音批量转录：{len(links)} 条", file=sys.stderr)
    print(f"  输入: {'参数'+str(len(args.urls)) if args.urls else ''}{' +文件'+str(len(links if args.file else [])) if args.file else ''}", file=sys.stderr)
    print("=" * 50, file=sys.stderr)

    # 阶段1：解析 video_id + 账本去重（跳过已转录的）
    todo = []  # (video_id, url)
    skipped = []
    for url in links:
        try:
            vid = extract_video_id(url)
        except Exception as e:
            print(f"  ❌ 无法解析链接（跳过）: {url} ({e})", file=sys.stderr)
            continue
        dup = ledger_check(vid, args.ledger_dir)
        if dup["exists"]:
            skipped.append((vid, url, dup["file_name"]))
            print(f"  ⏭ 已转录(跳过): {vid} -> {dup['file_name']}", file=sys.stderr)
        else:
            todo.append((vid, url))
    print(f"\n待转录 {len(todo)} 条，已转录跳过 {len(skipped)} 条", file=sys.stderr)

    if not todo:
        print("全部已转录或不可用，无需转录", file=sys.stderr)
        return 0

    work_dir = tempfile.mkdtemp(prefix="dyb-")
    metas, wav_map = {}, {}
    ok_list, failed = [], []

    try:
        # 阶段2：逐个下载音频（curl → ffmpeg 抽 wav），一条条过、不留多份
        print("\n[阶段2] 下载音频...", file=sys.stderr)
        for vid, url in todo:
            try:
                meta = fetch_video_meta(vid)
                audio = download_and_extract_audio(meta["video_url"], vid, work_dir)
                metas[vid] = meta
                wav_map[vid] = audio
                print(f"  ✓ 已下载: {vid} ({meta.get('title','')[:30]})", file=sys.stderr)
            except Exception as e:
                failed.append((vid, f"下载失败: {e}"))
                print(f"  ❌ 下载失败: {vid} ({e})", file=sys.stderr)

        # 阶段3：一次性加载模型，批量转录全部（复用一份模型）
        print("\n[阶段3] 批量转录（模型加载一次，复用 N 条）...", file=sys.stderr)
        trans = {}
        if wav_map:
            try:
                trans = transcribe_batch_audio(wav_map, work_dir)
            except Exception as e:
                for vid in wav_map:
                    failed.append((vid, f"批量转录失败: {e}"))

        # 阶段4：逐条整理 + 入库
        print("\n[阶段4] 整理+入库（每条 A1→A2/全文→markdown→原子写+账本）...", file=sys.stderr)
        for vid, _url in todo:
            if vid in failed or vid not in wav_map:
                continue
            raw = trans.get(vid, "")
            if not raw or raw.startswith("__ASR_ERROR__"):
                failed.append((vid, raw[12:] if raw.startswith("__ASR_ERROR__") else "转录为空(可能丢段)"))
                print(f"  ❌ 转录异常: {vid} ({raw[:60]})", file=sys.stderr)
                continue
            try:
                meta = metas[vid]
                cleaned = clean_text(raw)
                final_text, corrected, _nfix = organize_text(meta, cleaned, args.full_correct)
                markdown, safe_title = generate_markdown(meta, final_text, corrected)
                out_path = os.path.join(args.output_dir, f"{safe_title}.md")
                atomic_write(out_path, markdown)
                sha = content_sha256(markdown)
                ledger_write(vid, sha, out_path, args.ledger_dir)
                ok_list.append(out_path)
                print(f"  ✓ 入库: {safe_title[:40]}", file=sys.stderr)
            except Exception as e:
                failed.append((vid, f"整理/入库失败: {e}"))
                print(f"  ❌ {vid}: {e}", file=sys.stderr)

    finally:
        if not args.keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"  临时目录: {work_dir}", file=sys.stderr)

    # 汇报
    print("\n" + "=" * 50, file=sys.stderr)
    print(f"🏁 批量完成：成功 {len(ok_list)}，失败 {len(failed)}，跳过(已转录) {len(skipped)}", file=sys.stderr)
    for path in ok_list:
        print(f"  ✅ {path}", file=sys.stderr)
    if failed:
        print("失败：", file=sys.stderr)
        for vid, reason in failed:
            print(f"  ❌ {vid}: {reason}", file=sys.stderr)
    print("=" * 50, file=sys.stderr)

    # 持久化汇总（供监控脚本/一次性 cron 验证批量是否跑通）
    _persist_summary(args.ledger_dir, {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": len(links),
        "ok": ok_list,
        "failed": [{"video_id": v, "reason": r} for v, r in failed],
        "skipped": skipped,
    })

    # stdout：JSON 汇总（供飞书管道/脚本串联）
    print(json.dumps({
        "ok": ok_list,
        "failed": [v for v, _ in failed],
        "skipped": [v for v, _, _ in skipped],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
