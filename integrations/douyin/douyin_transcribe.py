#!/usr/bin/env python3
"""
抖音视频一键转录入库脚本

链路：抖音链接 → iesdouyin下载 → ffmpeg抽WAV → SenseVoice转录 → 清理 → LLM校对 → 生成Markdown → 入库

改进（蒸馏自 douyin-favorites-to-knowledge）：
  P1: SQLite 防重账本（替换 frontmatter 扫描，O(1) 查重 + SHA256 内容变化检测）
  P2: 原子写入（tempfile + fsync + os.replace，防中断产生坏文件）

用法：
    python douyin_transcribe.py "https://v.douyin.com/xxxxx/"
    python douyin_transcribe.py "https://v.douyin.com/xxxxx/" --output-dir "<data_root>/00-INBOX"
    python douyin_transcribe.py "https://v.douyin.com/xxxxx/" --no-llm-correct   # 跳过LLM校对（仅限脚本内部降级，不要手动加！）
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# 本地确定性纠错词表（A1，2026-08-20；与脚本同目录，-I 模式下需显式加 sys.path）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from correction_glossary import apply_glossary

# ============================================================
# 配置
# ============================================================

# ── 配置解析（环境变量 > 项目 config.yaml > 默认）────────────────────
# 抖音转录依赖独立的 ASR 环境（funasr + torch + SenseVoice 模型），
# 与知识库引擎的 venv 分开；这里不硬编码，交给 config.yaml 或环境变量。
def _resolve_paths():
    repo_root = Path(__file__).resolve().parents[2]
    cfg = {}
    _sp = str(repo_root / "scripts")
    if _sp not in sys.path:
        sys.path.insert(0, _sp)
    try:
        from config import get as _cfg_get, data_root  # type: ignore
        cfg = {
            "asr_python": str(_cfg_get("integrations.douyin.python", "") or ""),
            "chubby_script": str(_cfg_get("integrations.douyin.download_script", "") or ""),
            "out_dir": str(_cfg_get("integrations.douyin.output_dir", "") or ""),
            "data": str(data_root()),
        }
    except Exception:
        pass
    data = cfg.get("data") or str(repo_root / "vault")
    asr_python = (os.environ.get("ASR_PYTHON", "").strip()
                  or cfg.get("asr_python") or sys.executable)
    chubby = (os.environ.get("CHUBBY_DOWNLOAD_SCRIPT", "").strip()
              or cfg.get("chubby_script") or "")
    out_dir = (os.environ.get("DOUYIN_OUTPUT_DIR", "").strip()
               or cfg.get("out_dir") or str(Path(data) / "00-INBOX"))
    ledger = str(Path(data) / ".llm-wiki")
    return asr_python, chubby, out_dir, ledger


# ASR 环境解释器（跑解码/转录子进程用）
ASR_PYTHON, CHUBBY_DOWNLOAD_SCRIPT, DEFAULT_OUTPUT_DIR, DEFAULT_LEDGER_DIR = _resolve_paths()

# LLM 校对（可选增强；未配置 LLM 时自动跳过）
LLM_CORRECT_ENABLED = True  # 设为 False 可全局关闭

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)


def _gateway_session_headers(base_url: str) -> dict:
    """opencode.ai Zen/Go 网关强制要求 x-opencode-session，缺失直接 400
    {"type":"MissingSessionID"}（2026-10-14 起）。值只需不透明；
    与 scripts/llm_api.py 同一处理方式。其他网关返回空 dict。"""
    if "opencode.ai" not in (base_url or "").lower():
        return {}
    return {"x-opencode-session": f"oneshot-{os.urandom(8).hex()}"}


# ============================================================
# P1: SQLite 防重账本
# ============================================================

def _ledger_path(ledger_dir: str) -> str:
    """返回账本 DB 路径，确保目录存在"""
    os.makedirs(ledger_dir, exist_ok=True)
    return os.path.join(ledger_dir, "douyin_ledger.db")


def _init_ledger(db_path: str) -> None:
    """初始化账本表（幂等）"""
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promotions (
                video_id    TEXT PRIMARY KEY,
                content_sha256 TEXT NOT NULL,
                file_name   TEXT NOT NULL,
                promoted_at TEXT NOT NULL
            )
        """)
        conn.commit()


def ledger_check(video_id: str, ledger_dir: str) -> dict:
    """查询账本，返回 {'exists': bool, 'content_sha256': str, 'file_name': str}

    - exists=False：未转录过，可以继续
    - exists=True：已转录过，file_name 指向已有文件
    """
    db_path = _ledger_path(ledger_dir)
    _init_ledger(db_path)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        row = conn.execute(
            "SELECT content_sha256, file_name FROM promotions WHERE video_id=?",
            (video_id,)
        ).fetchone()
    if row:
        return {"exists": True, "content_sha256": row[0], "file_name": row[1]}
    return {"exists": False, "content_sha256": "", "file_name": ""}


def ledger_write(video_id: str, content_sha256: str, file_name: str, ledger_dir: str) -> None:
    """转录成功后写入账本（事务保护）"""
    db_path = _ledger_path(ledger_dir)
    _init_ledger(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        # 二次确认：写入前再查一次，防止并发重复
        existing = conn.execute(
            "SELECT 1 FROM promotions WHERE video_id=?", (video_id,)
        ).fetchone()
        if existing:
            conn.execute("COMMIT")
            return
        conn.execute(
            "INSERT INTO promotions VALUES (?, ?, ?, ?)",
            (video_id, content_sha256, file_name, now),
        )
        conn.commit()


def content_sha256(text: str) -> str:
    """计算文本内容的 SHA256（用于变化检测）"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ============================================================
# P2: 原子写入
# ============================================================

def atomic_write(path: str, content: str) -> None:
    """原子写入文件：先写临时文件 → fsync → os.replace

    os.replace 在同一文件系统上是原子操作，+fsync，要么完整替换要么不动，
    不会出现写到一半的坏文件。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(target))
    except BaseException:
        # 写入失败：清理临时文件，不影响原有文件
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


# ============================================================
# Step 1: 解析抖音链接，获取视频ID
# ============================================================

def extract_video_id(url: str) -> str:
    """从抖音短链接或完整链接提取视频ID"""
    if 'v.douyin.com' in url:
        result = subprocess.run(
            ["curl", "-sI", "-L", "-H", f"User-Agent: {MOBILE_UA}", url],
            capture_output=True, text=True, timeout=15
        )
        for line in result.stdout.split('\n'):
            if 'location:' in line.lower():
                url = line.split(':', 1)[1].strip()
                break
    m = re.search(r'/video/(\d+)', url)
    if m:
        return m.group(1)
    raise ValueError(f"无法从链接提取视频ID: {url}")


# ============================================================
# Step 2: 获取视频元信息（标题、作者、标签）
# 多级降级：①iesdouyin 分享页 _ROUTER_DATA（旧路径，兼容恢复）
#          ②playwright 渲染 www.douyin.com 拦截 aweme/detail API（2026-08-17 抖音改版后主路径）
# ============================================================

def fetch_video_meta(video_id: str) -> dict:
    """获取视频元信息，多级降级：
    1. iesdouyin 分享页 _ROUTER_DATA（2026-08 改版后已无 videoInfoRes，保留兼容）
    2. playwright 渲染 www.douyin.com，拦截 aweme/v1/web/aweme/detail API（当前主路径）
    """
    try:
        return _fetch_video_meta_iesdouyin(video_id)
    except Exception:
        pass
    return _fetch_video_meta_playwright(video_id)


def _fetch_video_meta_iesdouyin(video_id: str) -> dict:
    """旧路径：从iesdouyin分享页获取视频元信息（2026-08-17 起页面不再含视频数据，仅保留兼容）"""
    share_url = f"https://www.iesdouyin.com/share/video/{video_id}"
    result = subprocess.run(
        ["curl", "-s", "-L", "-H", f"User-Agent: {MOBILE_UA}", share_url],
        capture_output=True, timeout=30
    )
    html = result.stdout.decode('utf-8', errors='replace')

    match = re.search(r'window\._ROUTER_DATA\s*=\s*(.*?)</script>', html, re.DOTALL)
    if not match:
        raise RuntimeError("页面中未找到 _ROUTER_DATA")

    data = json.loads(match.group(1).strip())
    item = data["loaderData"]["video_(id)/page"]["videoInfoRes"]["item_list"][0]

    title = item.get("desc", "Untitled")
    author = item.get("author", {}).get("nickname", "")
    tags = [t.get("hashtag_name", '') for t in item.get("text_extra", []) if t.get('hashtag_name')]
    video_url = item["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
    # 真实视频时长（毫秒→秒）
    duration_ms = item.get("video", {}).get("duration", 0)
    duration_s = round(duration_ms / 1000) if duration_ms else 0

    return {
        "video_id": video_id,
        "title": title,
        "author": author,
        "tags": tags,
        "video_url": video_url,
        "duration": duration_s,
        "source_url": f"https://www.douyin.com/video/{video_id}",
    }


def _fetch_video_meta_playwright(video_id: str) -> dict:
    """playwright 渲染 www.douyin.com 视频页，拦截 aweme/v1/web/aweme/detail API 获取完整 meta。

    背景（2026-08-17 实测）：iesdouyin/m.douyin 分享页的 _ROUTER_DATA 不再含视频数据，
    www.douyin.com 有 byted_acrawler 反爬（需执行 JS 签名），纯 curl 无法拿到。
    本路径用系统 Edge（channel=\"msedge\"）真实渲染，页面加载时浏览器自动携带签名
    调用 aweme/detail API，从响应中提取与旧结构一致的字段。

    依赖：asr-env 已装 playwright（pip install playwright）；需系统装有 Edge/Chrome。
    """
    from playwright.sync_api import sync_playwright

    captured = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        try:
            ctx = browser.new_context(locale="zh-CN")
            page = ctx.new_page()

            def on_response(resp):
                if "aweme/v1/web/aweme/detail" in resp.url:
                    try:
                        captured["detail"] = resp.json()
                    except Exception:
                        pass

            page.on("response", on_response)
            try:
                page.goto(
                    f"https://www.douyin.com/video/{video_id}",
                    timeout=45000, wait_until="domcontentloaded",
                )
            except Exception:
                pass
            # 最多等 40s 让 API 响应到达
            for _ in range(40):
                if captured.get("detail"):
                    break
                page.wait_for_timeout(1000)
        finally:
            browser.close()

    if not captured.get("detail"):
        raise RuntimeError("playwright 未能捕获 aweme detail API 响应（抖音风控或页面结构再改）")

    data = captured["detail"]
    item = data.get("aweme_detail") or data.get("item_list", [{}])[0]
    if not item:
        raise RuntimeError("aweme detail API 响应中没有视频数据")

    title = item.get("desc", "Untitled")
    author = (item.get("author") or {}).get("nickname", "")
    tags = [t.get("hashtag_name", "") for t in item.get("text_extra", []) if t.get("hashtag_name")]
    video_obj = item.get("video") or {}
    play_addr = video_obj.get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    video_url = ""
    for u in url_list:
        if u.startswith("http"):
            video_url = u
            break
    video_url = video_url.replace("playwm", "play")
    if not video_url:
        raise RuntimeError("aweme detail API 响应中没有播放地址")
    duration_ms = video_obj.get("duration", 0)
    duration_s = round(duration_ms / 1000) if duration_ms else 0

    return {
        "video_id": video_id,
        "title": title,
        "author": author,
        "tags": tags,
        "video_url": video_url,
        "duration": duration_s,
        "source_url": f"https://www.douyin.com/video/{video_id}",
    }


# ============================================================
# Step 3: 下载视频 + ffmpeg抽WAV
# ============================================================

def download_and_extract_audio(video_url: str, video_id: str, work_dir: str) -> str:
    """下载视频并提取16kHz单声道WAV音频"""
    video_path = os.path.join(work_dir, f"{video_id}.mp4")
    audio_path = os.path.join(work_dir, f"{video_id}.wav")

    # 下载视频
    print(f"  ⬇ 下载视频...", file=sys.stderr)
    subprocess.run(
        ["curl", "-s", "-L", "-o", video_path,
         "-H", f"User-Agent: {MOBILE_UA}",
         "-H", "Referer: https://www.douyin.com/",
         video_url],
        timeout=300, check=True
    )
    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    print(f"  ✓ 视频 {size_mb:.1f}MB", file=sys.stderr)

    # ffmpeg 抽 16kHz 单声道 WAV
    print(f"  🎵 提取音频...", file=sys.stderr)
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path,
         "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path],
        capture_output=True, timeout=60, check=True
    )

    # 删除视频文件，只保留音频
    os.remove(video_path)
    audio_mb = os.path.getsize(audio_path) / (1024 * 1024)
    print(f"  ✓ 音频 {audio_mb:.1f}MB", file=sys.stderr)

    return audio_path


# ============================================================
# Step 4: SenseVoiceSmall 转录
# ============================================================

def transcribe_audio(audio_path: str, work_dir: str) -> tuple:
    """用 SenseVoiceSmall 转录音频，返回 (文本, 耗时秒)"""
    print(f"  🎙️ SenseVoice转录中...", file=sys.stderr)

    # 用临时文件传递结果，避免stdout被funasr日志污染
    result_file = os.path.join(work_dir, 'asr_result.json')

    script = '''
import sys, time, json, os
os.environ['TQDM_DISABLE'] = '1'

from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess
# 本地缓存优先（2026-08-29 修复：modelscope 目录树接口服务端故障导致 hub ID 版本校验必失败；
# 缓存缺失时仍回退 hub 下载。备份：douyin_transcribe.py.bak_20260829）
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

t0 = time.time()
result = model.generate(
    input=r'%s',
    language='zh',
    use_itn=True,
    batch_size_s=60,
)
elapsed = time.time() - t0

text = ''
if result:
    for r in result:
        if 'text' in r:
            text += rich_transcription_postprocess(r['text']) + '\\n\\n'

# 写结果到文件，而不是stdout
out = {'text': text.strip(), 'elapsed': round(elapsed, 1)}
with open(r'%s', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False)
''' % (audio_path.replace('\\', '\\\\'), result_file.replace('\\', '\\\\'))

    result = subprocess.run(
        [ASR_PYTHON, "-I", "-c", script],
        timeout=600,
    )

    if result.returncode != 0:
        raise RuntimeError(f"转录失败，返回码: {result.returncode}")

    if not os.path.exists(result_file):
        raise RuntimeError("转录结果文件未生成")

    with open(result_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['text'], data['elapsed']


# ============================================================
# Step 5: 清理文本（去emoji、去重复词、去语气词）
# ============================================================

def clean_text(text: str) -> str:
    """清理转录文本"""
    # 1. 去掉emoji（SenseVoice的语音情感识别输出）
    text = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF\U0000FE00-\U0000FE0F\U0000200D]+', '', text)

    # 2. 去掉连续重复的短词（口吃现象：我们我们、就就就）
    text = re.sub(r'([\u4e00-\u9fff]{1,3})\1+', r'\1', text)

    # 3. 去掉句末多余语气词堆积（啊啊、好吧好吧）
    text = re.sub(r'(啊[。！？]?){2,}', '啊', text)
    text = re.sub(r'(好吧[。！？]?){2,}', '好吧', text)

    # 4. 清理多余空格和换行
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text


# ============================================================
# Step 6: LLM 校对（修正专业术语谐音错误）
# ============================================================

def _load_hermes_llm_config():
    """从 Hermes config.yaml 读取当前 model provider 的 base_url/api_key/model（自动识别，不硬编码）

    回退链：环境变量 LLM_API_BASE/KEY/MODEL → Hermes config.yaml（model + custom_providers）
           → Hermes auth.json + .env（非 custom: 前缀 provider，如 opencode-go）
    与 convert.py 的自动回退机制保持一致，换 API 无需改脚本。
    """
    # 1. 环境变量优先
    base = os.environ.get("LLM_API_BASE", "").strip()
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if base and key:
        return base.rstrip("/"), key, model or ""

    # 2. 回退：Hermes config.yaml
    provider_name = ""
    default_model = ""
    try:
        import yaml
        from pathlib import Path as _Path
        config_paths = [
            _Path(os.environ.get("HERMES_HOME", "")) / "config.yaml",
            _Path.home() / "AppData" / "Local" / "hermes" / "config.yaml",
            _Path.home() / ".hermes" / "config.yaml",
        ]
        for cp in config_paths:
            if cp.exists():
                with open(cp, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                if not cfg:
                    continue
                model_cfg = cfg.get("model", {})
                if not provider_name:
                    provider_name = model_cfg.get("provider", "")
                base_url = model_cfg.get("base_url", "")
                api_key = model_cfg.get("api_key", "")
                if not default_model:
                    default_model = model_cfg.get("default", "")
                if provider_name.startswith("custom:"):
                    custom_name = provider_name.split(":", 1)[1]
                    for p in cfg.get("custom_providers", cfg.get("providers", [])):
                        if p.get("name", "").lower() == custom_name.lower():
                            base_url = base_url or p.get("base_url", "")
                            api_key = api_key or p.get("api_key", "")
                            default_model = default_model or p.get("model", "")
                            break
                if base_url and api_key:
                    return base_url.rstrip("/"), api_key, default_model
    except Exception:
        pass

    # 3. 回退：Hermes auth.json + .env（非 custom: 前缀 provider，如 opencode-go）
    #    auth.json 存 provider 条目（base_url + label/source 指向环境变量名），
    #    密钥本身在 profiles/<profile>/.env 或 ~/AppData/Local/hermes/.env
    if provider_name and not provider_name.startswith("custom:"):
        try:
            from pathlib import Path as _Path
            auth_paths = [
                _Path(os.environ.get("HERMES_HOME", "")) / "auth.json",
                _Path.home() / "AppData" / "Local" / "hermes" / "auth.json",
            ]
            env_paths = [
                _Path(os.environ.get("HERMES_HOME", "")) / ".env",
                _Path.home() / "AppData" / "Local" / "hermes" / ".env",
            ]
            for ap in auth_paths:
                if not ap.exists():
                    continue
                try:
                    auth = json.loads(ap.read_text(encoding="utf-8"))
                except Exception:
                    continue
                entries = auth.get(provider_name, [])
                # Hermes auth.json 结构：{credential_pool: {name: [...]}} 或 {providers: {name: [...]}}
                if not entries:
                    entries = (auth.get("credential_pool") or {}).get(provider_name, [])
                if not entries:
                    entries = (auth.get("providers") or {}).get(provider_name, [])
                if isinstance(entries, dict):
                    entries = [entries]
                if not entries:
                    continue
                entry = entries[0]
                base_url = (entry.get("base_url") or "").strip()
                # 环境变量名：label 形如 OPENCODE_GO_API_KEY，或 source 形如 env:XXX
                env_key_name = ""
                label = (entry.get("label") or "").strip()
                if label.isupper() and "_" in label:
                    env_key_name = label
                src = (entry.get("source") or "").strip()
                if src.startswith("env:"):
                    env_key_name = src[4:].strip()
                api_key = os.environ.get(env_key_name, "").strip() if env_key_name else ""
                if not api_key and env_key_name:
                    for ep in env_paths:
                        if ep.exists():
                            for line in ep.read_text(encoding="utf-8", errors="ignore").splitlines():
                                line = line.strip()
                                if line.startswith(env_key_name + "="):
                                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                                    break
                        if api_key:
                            break
                if base_url and api_key:
                    return base_url.rstrip("/"), api_key, default_model or ""
        except Exception:
            pass
    return "", "", ""


def _resolve_api_model(model_name: str, base_url: str) -> str:
    """把 Hermes 别名/配置模型名映射为厂商 API 真实模型名。

    Hermes config 里的模型名（如 deepseek-v4-flash）是网关别名，
    直接调厂商 OpenAI 兼容 API 时会返回空内容。这里做映射兜底：
    - DeepSeek: deepseek-v4-flash → deepseek-chat, deepseek-v4-pro → deepseek-reasoner
    - 未知模型名保留原样（由 API 自己报错，或回退重试逻辑兜底）
    """
    m = (model_name or "").strip().lower()
    b = (base_url or "").lower()
    if "deepseek.com" in b or "deepseek" in b:
        if m in ("deepseek-v4-flash", "deepseek-v4", "deepseek-chat"):
            return "deepseek-chat"
        if m in ("deepseek-v4-pro", "deepseek-v4-pro[1m]", "deepseek-reasoner", "deepseek-r1"):
            return "deepseek-reasoner"
    return model_name


def _llm_candidate_models(model_name: str, base_url: str) -> list:
    """候选模型：主模型 + 仅当解析成厂商真名时才追加同厂备选。

    opencode.go 等网关（如 opencode.ai/zen/go）只认自家别名（deepseek-v4-flash）；
    硬追加的 deepseek-chat/deepseek-reasoner 对它必然 401，且会掩盖主模型真实失败原因。
    仅当 _resolve_api_model 把别名映射成了厂商真名（如 deepseek.com → deepseek-chat）
    时，才追加同厂备选做回退。
    """
    resolved = _resolve_api_model(model_name, base_url)
    primary = resolved or "deepseek-chat"
    candidates = [primary]
    if resolved and resolved != (model_name or ""):
        for fb in ("deepseek-chat", "deepseek-reasoner"):
            if fb not in candidates:
                candidates.append(fb)
    # 2026-09-07: commandcode 网关（api.commandcode.ai）下 deepseek-v4-flash 是
    # thinking 模型且 thinking disabled 不生效，长文本时 reasoning 挤爆 max_tokens
    # （8192→16000 仍 content 空，24000+ 网关 524 超时）。同网关的
    # deepseek/deepseek-v4-flash-fast 实测 reasoning≈85 正常输出，作为兜底候选追加。
    if "commandcode.ai" in (base_url or "").lower() and "flash-fast" not in primary:
        candidates.append("deepseek/deepseek-v4-flash-fast")
    return candidates


def llm_correct_and_segment(text: str, title: str, tags: list) -> str:
    """用LLM校对转录文本（修正术语+按主题分段），一步到位"""

    if not LLM_CORRECT_ENABLED:
        return text

    tags_str = '、'.join(tags) if tags else '无'
    prompt = f"""你是一个中文转录校对和整理专家。以下是一段视频的ASR转录文本，需要你做两件事：

**任务1：修正谐音错误**
根据标题和标签的上下文，修正转录文本中的谐音错误。只修正明显错误，不要改变原文的口语风格和语序。

校对原则：
- ASR会把不常见的词识别成发音相近的常见词，形成"合法但意思错误"的替换
- **校对时以通用规则为准：凡是发音相近、替换后语法通顺但语境不对的，都要修正。不要只盯着特定分类去查，任何类型的谐音错误都要处理**
- **视频标签和分类仅用于辅助判断上下文语境，不限制校对范围。视频标签是A不代表内容只涉及A，可能穿插B、C领域的术语，都要校对**
- 不能只看语法通顺就放过，还要结合上下文语境判断用词是否准确
- 只改明显错误，不确定的不改

以下分类仅为辅助参考，帮助识别容易出错的场景，不限于这些：
- 技术术语：英文专有名词最易错认（如RAG→RG、Claude→cloud）
- 经典原文：古文/佛经/诗词引用中，发音相近的字被替换（如"同归于寂"→"同归于尽"）
- 方言/口语词：地方俗语、网络用语被识别成标准普通话（如"社牛"→"射牛"）
- 人名/地名：不常见的人名地名被替换成常见同音字
- 行业黑话：特定领域的行话被识别成日常用语

**任务2：按主题分段**
将转录文本按话题/主题分成若干段落，每段加一个简洁的小标题（## 二级标题）。不要删减内容，不要改写，只是分段和加小标题。

视频标题：{title}
视频标签：{tags_str}

转录原文：
{text}

请直接输出校对+分段后的文本，不要加任何解释或标注。"""

    print(f"  🔍 LLM校对+分段中...", file=sys.stderr)

    # 自动识别API配置（env → Hermes config），不硬编码任何厂商
    base_url, api_key, model_name = _load_hermes_llm_config()
    if not (base_url and api_key):
        print(f"  ⚠ LLM校对失败: 未找到可用API配置（LLM_API_BASE/KEY 或 Hermes config.yaml）", file=sys.stderr)
        return text

    # 候选模型：优先映射真实API模型名，网关别名场景仅用主模型（追加的厂内备选会对网关401）
    candidate_models = _llm_candidate_models(model_name, base_url)

    import urllib.request
    import json as _json

    last_err = ""
    for model in candidate_models:
        try:
            req_body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                # 2026-09-07: 8192 → 16000。commandcode 网关实测 thinking disabled 不生效，
                # max_tokens 是 reasoning+content 总预算，8192 会被 reasoning 挤爆导致 content 空。
                # 16000 下 reasoning 6738 + content 3209 完整输出（finish=stop）。
                "max_tokens": 16000,
            }
            # deepseek-v4 系列是 thinking 模型：长文本时 reasoning 会耗尽
            # completion tokens 导致 content 为空（finish_reason=length）。
            # 校对小任务无需思考，显式关闭。opencode.go 网关特有模型名。
            # 2026-09-07：模型名可能带 provider 前缀（deepseek/deepseek-v4-flash），
            # startswith 判断需兼容前缀变体。注意：commandcode 网关实测 thinking disabled
            # 不生效（usage 仍含 reasoning_tokens），但无害保留。
            if model.startswith("deepseek-v4") or "/deepseek-v4" in model:
                req_body["thinking"] = {"type": "disabled"}
            req_data = _json.dumps(req_body).encode('utf-8')

            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    # opencode.ai 等网关拒绝 urllib 默认 UA（Python-urllib/3.x → 403）
                    "User-Agent": MOBILE_UA,
                    # opencode.ai 网关强制会话头（缺失 → 400 MissingSessionID）
                    **_gateway_session_headers(base_url),
                }
            )

            with urllib.request.urlopen(req, timeout=180) as resp:
                result = _json.loads(resp.read().decode('utf-8'))
                corrected = result['choices'][0]['message']['content'].strip()
            if corrected:
                print(f"  ✓ LLM校对+分段完成 (model={model})", file=sys.stderr)
                return corrected
            # 内容为空 → 换模型重试
            last_err = f"model={model} 返回空内容"
        except Exception as e:
            last_err = f"model={model}: {e}"
            continue

    print(f"  ⚠ LLM校对失败({last_err})，使用原始转录", file=sys.stderr)
    return text


# ============================================================
# A2: patch 式 LLM 整理（可选模式 --patch-mode，默认关）
# 机器预整理 → LLM 只出增量 patch → 脚本装配渲染，正文逐字保留
# ============================================================

def preorganize_segments(text: str, target: int = 6) -> list:
    """机器预整理：切句 → 合并碎句 → 按目标块数分块（零 LLM，纯规则）。

    只做确定性分块，不改文字。LLM 后续只为每块起小标题、出修正补丁。
    """
    if not text:
        return []
    # 1) 在中文句末标点后切分（保留标点）
    parts = [p.strip() for p in re.split(r'(?<=[。！？!?…])', text) if p.strip()]
    # 2) 合并过短碎片（<4 字）进前一句，避免碎句当独立块
    merged = []
    for p in parts:
        if len(p) < 4 and merged:
            merged[-1] += p
        else:
            merged.append(p)
    if not merged:
        return []
    # 3) 均匀分块
    n = len(merged)
    k = min(max(target, 1), n)
    base, carry = divmod(n, k)
    blocks, idx = [], 0
    for i in range(k):
        size = base + (1 if i < carry else 0)
        blocks.append(''.join(merged[idx:idx + size]).strip())
        idx += size
    return [b for b in blocks if b]


def _parse_patch_json(content: str):
    """鲁棒解析 LLM 返回的 patch JSON；失败返回 None。"""
    import json as _json
    if not content:
        return None
    t = content.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        return _json.loads(t)
    except Exception:
        pass
    a, b = t.find('{'), t.rfind('}')
    if a != -1 and b > a:
        try:
            return _json.loads(t[a:b + 1])
        except Exception:
            pass
    return None


def llm_patch_and_segment(blocks: list, title: str, tags: list):
    """让 LLM 只输出增量 patch（headings + fixes + paragraph_edits），不重写全文。

    返回 patch dict；全部失败返回 None（由调用方回退全文校对）。
    """
    if not LLM_CORRECT_ENABLED or not blocks:
        return None
    tags_str = '、'.join(tags) if tags else '无'
    seq = '\n\n'.join(f"[段落{i}]\n{b}" for i, b in enumerate(blocks, 1))
    prompt = f"""你是中文转录校对专家。以下是按段分割的 ASR 转录文本（段间用[段落N]分隔）。请只输出一个 JSON patch，**不要重写全文**：

1. headings：给每段一个简洁的语义化小标题（数组索引与段落序号一一对应，≤15字）
2. fixes：修正谐音/术语错误的词或短语清单，每项 {{"from":"被改的原文片段","to":"正确写法","confidence":"high或low","section":段落序号}}
   - 原则：ASR 把不常见词识别成发音相近的常见词；只改明显错误，不确定的标记 confidence 为 low
   - 专业术语/人名、以及标题「{title}」标签「{tags_str}」相关词汇优先
   - 古文/经典名句/诗词引用：当某句疑似引用古文经典时，**对照经典原文校验**（如庄子《秋水》"井蛙不可以语于海者""夏虫不可以语于冰者，笃于时也""曲士不可以语于道者"），勿把"语于"错成"与于"、"冰"错成"兵"、"时"错成"食"，以原文为准
   - confidence=high 表示该词可安全全文替换；low 表示仅限该段局部
3. paragraph_edits：仅当某整段需要结构改写时才给 {{"section":段落序号,"replace":"整段完整替换文字"}}（一般不要使用）

只输出 JSON，不要马甲、不要解释、不要 ``` 包裹。格式：
{{"headings":["小标题1","小标题2"],"fixes":[{{"from":"...","to":"...","confidence":"high","section":1}}],"paragraph_edits":[]}}

段落文本：
{seq}"""
    base_url, api_key, model_name = _load_hermes_llm_config()
    if not (base_url and api_key):
        print("  ⚠ patch模式: 未找到可用API配置", file=sys.stderr)
        return None
    candidate_models = _llm_candidate_models(model_name, base_url)
    import urllib.request
    last_err = ""
    for model in candidate_models:
        try:
            req_body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 4096,
            }
            if model.startswith("deepseek-v4"):
                req_body["thinking"] = {"type": "disabled"}
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(req_body).encode('utf-8'),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": MOBILE_UA,
                    # opencode.ai 网关强制会话头（缺失 → 400 MissingSessionID）
                    **_gateway_session_headers(base_url),
                }
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            content = result['choices'][0]['message']['content'].strip()
            patch = _parse_patch_json(content)
            if patch is not None and patch.get("headings"):
                print(f"  ✓ patch式整理: {len(blocks)}段 / 标题{len(patch.get('headings', []))} / 修正{len(patch.get('fixes', []))} (model={model})", file=sys.stderr)
                return patch
            last_err = f"{model} patch JSON 解析失败"
        except Exception as e:
            last_err = f"{model}: {e}"
            continue
    print(f"  ⚠ patch模式失败({last_err})", file=sys.stderr)
    return None


def apply_patch(blocks: list, patch: dict) -> str:
    """按 patch 装配最终正文：应用修正 + 小标题，正文逐字保留。

    - high confidence 修正 → 全文替换
    - low confidence 修正 → 仅替换到指定段落
    - paragraph_edits → 整段替换（优先于局部修正）
    """
    headings = patch.get("headings") or []
    fixes = patch.get("fixes") or []
    p_edits = {}
    for e in (patch.get("paragraph_edits") or []):
        sec, rp = e.get("section"), e.get("replace")
        if sec is not None and rp and rp.strip():
            p_edits[int(sec)] = rp.strip()

    nb = list(blocks)
    # 1) 局部（low confidence）修正
    for f in fixes:
        fr = (f.get("from") or "").strip(); to = (f.get("to") or "").strip()
        if not fr or not to or len(fr) > 100:
            continue
        if (f.get("confidence") or "").lower() != "high":
            sec = f.get("section")
            if sec is not None:
                i = int(sec)
                if 1 <= i <= len(nb) and i not in p_edits:
                    nb[i - 1] = nb[i - 1].replace(fr, to)
    # 2) 整段替换
    for i, rv in p_edits.items():
        if 1 <= i <= len(nb):
            nb[i - 1] = rv
    # 3) 全局（high confidence）修正
    for f in fixes:
        fr = (f.get("from") or "").strip(); to = (f.get("to") or "").strip()
        if not fr or not to or len(fr) > 100:
            continue
        if (f.get("confidence") or "").lower() == "high":
            nb = [b.replace(fr, to) for b in nb]

    out = []
    for i, blk in enumerate(nb, 1):
        h = headings[i - 1].strip() if (i - 1) < len(headings) and headings[i - 1].strip() else f"段落{i}"
        out.append(f"## {h}\n\n{blk.strip()}\n")
    return "\n".join(out)


# ============================================================
# Step 7: 生成结构化 Markdown
# ============================================================

def generate_markdown(meta: dict, transcript: str, corrected: bool) -> str:
    """生成带 frontmatter 的结构化 Markdown"""
    now = datetime.now().strftime("%Y-%m-%d")
    tags_yaml = ', '.join(['抖音'] + meta['tags'])

    # 清理标题（去掉#标签、emoji、只取第一句）
    clean_title = meta['title']
    # 去掉标题里的#标签部分
    clean_title = re.sub(r'\s*#[^#]*$', '', clean_title)
    # 去掉所有emoji
    clean_title = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF\U0000FE00-\U0000FE0F\U0000200D]+', '', clean_title)
    clean_title = clean_title.strip()
    # 只取第一句（抖音很多人把正文写在标题里）
    first_sentence = re.split(r'[，。！？\n]', clean_title)[0].strip()
    if 5 <= len(first_sentence) < len(clean_title) and len(first_sentence) <= 30:
        clean_title = first_sentence
    if not clean_title:
        clean_title = meta['title']

    # 安全文件名
    safe_title = ''.join(c for c in clean_title if c.isalnum() or c in '《》-_ ').strip()[:50]

    markdown = f"""---
title: {clean_title}
type: note
platform: douyin
tags: [{tags_yaml}]
created: {now}
source: {meta['source_url']}
author: {meta['author']}
video_id: {meta['video_id']}
duration: {meta['duration']}s
transcriber: SenseVoiceSmall
llm_corrected: {'true' if corrected else 'false'}
---

# {clean_title}

> 转录引擎：SenseVoiceSmall | LLM校对：{'是' if corrected else '否'}

## 逐字稿

{transcript}
"""
    return markdown, safe_title


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='抖音视频一键转录入库')
    parser.add_argument('url', help='抖音分享链接')
    parser.add_argument('--output-dir', default=DEFAULT_OUTPUT_DIR, help='输出目录（默认00-INBOX）')
    parser.add_argument('--ledger-dir', default=DEFAULT_LEDGER_DIR, help='防重账本目录（默认.llm-wiki/）')
    parser.add_argument('--no-llm-correct', action='store_true', help='跳过LLM校对')
    parser.add_argument('--patch-mode', action='store_true', help='[兼容保留] 显式走 patch 式整理（当前已是默认，此参数可省略）')
    parser.add_argument('--full-correct', action='store_true', help='强制用旧全文校对（默认走 patch 式 A2，patch 失败自动回退旧流程）')
    parser.add_argument('--keep-temp', action='store_true', help='保留临时文件（调试用）')
    args = parser.parse_args()

    # 全局开关
    global LLM_CORRECT_ENABLED
    if args.no_llm_correct:
        LLM_CORRECT_ENABLED = False

    print("=" * 50, file=sys.stderr)
    print("抖音视频转录入库", file=sys.stderr)
    print("=" * 50, file=sys.stderr)

    # Step 1: 解析链接
    print("\n📍 Step 1: 解析链接...", file=sys.stderr)
    video_id = extract_video_id(args.url)
    print(f"  ✓ 视频ID: {video_id}", file=sys.stderr)

    # Step 1.5: 去重检查（SQLite 账本，O(1) 查询）
    print("\n🔍 Step 1.5: 去重检查（SQLite账本）...", file=sys.stderr)
    dup = ledger_check(video_id, args.ledger_dir)
    if dup["exists"]:
        print(f"  ⚠ 视频已转录过！文件: {dup['file_name']}", file=sys.stderr)
        print(f"DUPLICATE:{dup['file_name']}")
        return
    print(f"  ✓ 无重复", file=sys.stderr)

    # Step 2: 获取元信息
    print("\n📋 Step 2: 获取视频信息...", file=sys.stderr)
    meta = fetch_video_meta(video_id)
    print(f"  ✓ 标题: {meta['title'][:60]}", file=sys.stderr)
    print(f"  ✓ 作者: {meta['author']}", file=sys.stderr)
    print(f"  ✓ 标签: {meta['tags']}", file=sys.stderr)

    # Step 3: 下载 + 抽音频
    print("\n⬇ Step 3: 下载视频...", file=sys.stderr)
    work_dir = tempfile.mkdtemp(prefix='dyt-')
    try:
        audio_path = download_and_extract_audio(meta['video_url'], video_id, work_dir)

        # Step 4: SenseVoice 转录
        print("\n🎙️ Step 4: 转录...", file=sys.stderr)
        raw_text, elapsed = transcribe_audio(audio_path, work_dir)
        print(f"  ✓ 转录完成: {len(raw_text)}字, {elapsed:.0f}秒", file=sys.stderr)

        # Step 5: 清理文本
        print("\n🧹 Step 5: 清理文本...", file=sys.stderr)
        cleaned_text = clean_text(raw_text)
        print(f"  ✓ 清理完成: {len(cleaned_text)}字", file=sys.stderr)

        # Step 5.5: 规则纠错词表（确定性兜底层，LLM 前置，零 token 成本）
        glossed_text, gloss_fixes = apply_glossary(cleaned_text)
        if gloss_fixes:
            fix_str = ", ".join(f"{a}→{b}" for a, b in gloss_fixes)
            print(f"  ✓ 规则词表修正 {len(gloss_fixes)} 处: {fix_str}", file=sys.stderr)
        else:
            print(f"  – 规则词表无命中", file=sys.stderr)

        # Step 6: LLM 校对（默认 patch 式 A2；--full-correct 强制旧全文校对，patch 失败自动回退旧流程）
        corrected = False
        final_text = glossed_text
        if LLM_CORRECT_ENABLED:
            use_patch = args.patch_mode or not args.full_correct
            if use_patch:
                print("\n🔍 Step 6: patch式 LLM 整理（机器预整理→LLM出增量→装配渲染）...", file=sys.stderr)
                blocks = preorganize_segments(glossed_text)
                patch = llm_patch_and_segment(blocks, meta['title'], meta['tags']) if blocks else None
                if patch is not None:
                    final_text = apply_patch(blocks, patch)
                    corrected = final_text != glossed_text
                else:
                    # patch 失败 → 回退全文校对，保证校对不缺失
                    print("  ⚠ patch 失败，回退全文校对", file=sys.stderr)
                    final_text = llm_correct_and_segment(glossed_text, meta['title'], meta['tags'])
                    corrected = final_text != glossed_text
            else:
                print("\n🔍 Step 6: LLM校对+分段（全文模式）...", file=sys.stderr)
                final_text = llm_correct_and_segment(glossed_text, meta['title'], meta['tags'])
                # corrected 必须反映真实校对结果（llm_correct_and_segment 失败时返回原文）
                corrected = final_text != glossed_text

        # Step 7: 生成 Markdown
        print("\n📝 Step 7: 生成Markdown...", file=sys.stderr)
        markdown, safe_title = generate_markdown(meta, final_text, corrected)

        # P2: 原子写入（替换原来的 open().write()）
        output_path = os.path.join(args.output_dir, f"{safe_title}.md")
        atomic_write(output_path, markdown)

        # P1: 写入防重账本
        sha = content_sha256(markdown)
        ledger_write(video_id, sha, output_path, args.ledger_dir)

        print(f"\n{'=' * 50}", file=sys.stderr)
        print(f"✅ 完成！", file=sys.stderr)
        print(f"{'=' * 50}", file=sys.stderr)
        print(f"  标题: {meta['title'][:60]}", file=sys.stderr)
        print(f"  字数: {len(final_text)}", file=sys.stderr)
        print(f"  转录: {elapsed:.0f}秒", file=sys.stderr)
        # 转录密度校验（轻量：字/秒。中文口播正常约 3.5-6；显著偏低=可能丢段/漏语音）
        _vdur = meta.get("duration", 0) or 0
        if _vdur > 0:
            _dens = len(final_text) / _vdur
            if _dens < 2.0:
                print(f"  ⚠️ 转录密度异常偏低: {_dens:.1f} 字/秒（视频{_vdur}s 仅转录{len(final_text)}字，正常约3.5-6）→ 可能丢段/漏语音，建议抽查原视频", file=sys.stderr)
            else:
                print(f"  密度: {_dens:.1f} 字/秒（视频{_vdur}s/转录{len(final_text)}字，正常约3.5-6）", file=sys.stderr)
        else:
            print(f"  （无视频时长元数据，跳过密度校验）", file=sys.stderr)
        print(f"  校对: {'是' if corrected else '否'}", file=sys.stderr)
        print(f"  输出: {output_path}", file=sys.stderr)

        # stdout 输出路径（方便脚本串联）
        print(output_path)

    finally:
        if not args.keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"  临时文件: {work_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
