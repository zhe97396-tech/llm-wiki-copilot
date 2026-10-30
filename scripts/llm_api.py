#!/usr/bin/env python3
"""LLM API 统一配置与调用模块（convert.py / integrations 共用）

设计目标：**换 provider 零改脚本**。所有脚本通过本模块自动识别配置，无需每处维护。
LLM 是可选的：不配置时，翻译/中文标签等增强步骤自动跳过，核心链路照常运行。

回退链（load_llm_config）：
  1. 环境变量 LLM_API_BASE / LLM_API_KEY / LLM_MODEL
  2. 项目 config.yaml 的 llm 段（安装向导生成，推荐路径）
  3. 宿主兼容：Hermes config.yaml（model 段 + custom: 前缀 provider 的 custom_providers/providers）
  4. 宿主兼容：Hermes auth.json（credential_pool / providers 下的非 custom 前缀 provider）
     + .env 文件（key 实际存储位置，label/source 字段给出环境变量名）

调用层（call_llm）自动处理：
  - 浏览器 UA（opencode.ai 等网关拒绝 urllib 默认 UA → 403）
  - deepseek-v4 系列 thinking 关闭（长文本 reasoning 耗尽 token → content 为空）
  - 模型 fallback 链（主模型 → deepseek-chat → deepseek-reasoner）

用法：
    from llm_api import load_llm_config, resolve_api_model, call_llm
    base_url, api_key, model = load_llm_config()
    text = call_llm("prompt...", max_tokens=2048)
"""

import json
import os
import sys
import urllib.request
import uuid
from pathlib import Path

# 浏览器 UA：opencode.ai 等网关拒绝 urllib/requests 默认 UA（403）
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

# 会话亲和头（opencode.ai Zen/Go 网关）：2026-10-14 起强制要求
# x-opencode-session，缺失直接 400 {"type":"MissingSessionID"}。
# 值只需不透明且同一会话内一致（Hermes 主链路用 oneshot-<hex> 兜底），
# 用于把请求钉到同一上游后端以复用 prompt cache。
OPENCODE_SESSION_HEADER = "x-opencode-session"


def _opencode_session_headers(base_url: str) -> dict:
    """opencode.ai 网关必须带 x-opencode-session，其他网关返回空 dict。"""
    if "opencode.ai" not in (base_url or "").lower():
        return {}
    return {OPENCODE_SESSION_HEADER: f"oneshot-{uuid.uuid4().hex[:16]}"}


def load_llm_config() -> tuple[str, str, str]:
    """返回 (base_url, api_key, model_name)。识别失败返回 ("", "", "")。"""
    # 1. 环境变量优先
    base = os.environ.get("LLM_API_BASE", "").strip()
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if base and key:
        return base.rstrip("/"), key, model or ""

    # 2. 项目 config.yaml 的 llm 段（安装向导生成的唯一配置源，推荐）
    try:
        from config import get as _cfg_get
        cfg_base = str(_cfg_get("llm.base_url", "") or "").strip()
        cfg_key = str(_cfg_get("llm.api_key", "") or "").strip()
        cfg_model = str(_cfg_get("llm.model", "") or "").strip()
        if cfg_base and cfg_key:
            return cfg_base.rstrip("/"), cfg_key, cfg_model
    except Exception:
        pass

    # 3. 宿主兼容：Hermes config.yaml（model 段 + custom: 前缀 provider）
    provider_name = ""
    default_model = ""
    try:
        import yaml
    except ImportError:
        # 缺 PyYAML 时必须显式告警：否则下面的 except 会静默吞掉，
        # 表现为"LLM 步骤全部失败但脚本不报错"（如 #stars 标签全空）。
        print("  ⚠ 缺少 PyYAML，无法读取配置 → LLM 步骤将不可用。"
              "请用项目自带解释器运行（.venv/Scripts/python.exe 或 .venv/bin/python）",
              file=sys.stderr)
    else:
        try:
            config_paths = [
                Path(os.environ.get("HERMES_HOME", "")) / "config.yaml",
                Path.home() / "AppData" / "Local" / "hermes" / "config.yaml",
                Path.home() / ".hermes" / "config.yaml",
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

    # 3. auth.json + .env（非 custom: 前缀 provider，如 opencode-go）
    if provider_name and not provider_name.startswith("custom:"):
        try:
            auth_paths = [
                Path(os.environ.get("HERMES_HOME", "")) / "auth.json",
                Path.home() / "AppData" / "Local" / "hermes" / "auth.json",
            ]
            env_paths = [
                Path(os.environ.get("HERMES_HOME", "")) / ".env",
                Path.home() / "AppData" / "Local" / "hermes" / ".env",
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


def resolve_api_model(model_name: str, base_url: str) -> str:
    """把 Hermes 别名/配置模型名映射为厂商 API 真实模型名。

    - opencode.ai 等网关：deepseek-v4-flash 就是真实名，直接透传
    - DeepSeek 官方：deepseek-v4-flash → deepseek-chat, deepseek-v4-pro → deepseek-reasoner
    - 未知模型名保留原样（由 API 自己报错，或 fallback 链兜底）
    """
    m = (model_name or "").strip().lower()
    b = (base_url or "").lower()
    if "opencode.ai" in b or "zen" in b:
        return model_name  # 网关直传，不要映射
    if "deepseek.com" in b or "deepseek" in b:
        if m in ("deepseek-v4-flash", "deepseek-v4", "deepseek-chat"):
            return "deepseek-chat"
        if m in ("deepseek-v4-pro", "deepseek-v4-pro[1m]", "deepseek-reasoner", "deepseek-r1"):
            return "deepseek-reasoner"
    return model_name


def _llm_candidate_models(model_name: str, base_url: str) -> list:
    """候选模型：主模型 + 仅当解析成厂商真名时才追加同厂备选。

    opencode 等网关只认自家别名（如 deepseek-v4-flash）；
    硬追加的 deepseek-chat/deepseek-reasoner 对它必然 401，
    且会掩盖主模型真实失败原因。仅当 resolve_api_model 把别名
    映射成了厂商真名（如 deepseek.com → deepseek-chat）时才回退。
    与 douyin_transcribe._llm_candidate_models 同一方案（2026-08-21）。
    """
    resolved = resolve_api_model(model_name, base_url)
    candidates = [resolved or "deepseek-chat"]
    if resolved and resolved != (model_name or ""):
        for fb in ("deepseek-chat", "deepseek-reasoner"):
            if fb not in candidates:
                candidates.append(fb)
    return candidates


def call_llm(prompt: str, max_tokens: int = 8192, temperature: float = 0.1,
             timeout: int = 180) -> str:
    """统一 LLM 调用（OpenAI 兼容 /chat/completions）。

    自动处理：配置识别 → 浏览器 UA → thinking disabled（deepseek-v4 系列）
    → 模型 fallback 链（主模型必试；同厂备选仅在解析出厂商真名时追加）。

    返回响应文本；全部失败返回空字符串。

    max_tokens 默认 8192：commandcode 网关下 2048 会被长文本输出耗尽
    （finish_reason=length → content 为空）。调用方按需显式传小值。
    """
    base_url, api_key, model_name = load_llm_config()
    if not (base_url and api_key):
        print("  ⚠ LLM调用失败: 未找到可用API配置（LLM_API_BASE/KEY 或 Hermes config.yaml）", file=sys.stderr)
        return ""

    candidate_models = _llm_candidate_models(model_name, base_url)

    last_err = ""
    for model in candidate_models:
        try:
            req_body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            # deepseek-v4 系列是 thinking 模型：长文本时 reasoning 会耗尽
            # completion tokens 导致 content 为空（finish_reason=length）。
            # 小任务无需思考，显式关闭。
            # 兼容带 provider 前缀的模型名（如 deepseek/deepseek-v4-flash）
            if "deepseek-v4" in model:
                req_body["thinking"] = {"type": "disabled"}
            req_data = json.dumps(req_body).encode('utf-8')

            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    # 网关拒绝 urllib 默认 UA（Python-urllib/3.x → 403）
                    "User-Agent": MOBILE_UA,
                    # opencode.ai 网关强制会话头（缺失 → 400 MissingSessionID）
                    **_opencode_session_headers(base_url),
                }
            )

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                corrected = result['choices'][0]['message']['content'].strip()
            if corrected:
                return corrected
            # 内容为空 → 换模型重试
            last_err = f"model={model} 返回空内容"
        except Exception as e:
            last_err = f"model={model}: {e}"
            continue

    print(f"  ⚠ LLM调用失败({last_err})，使用默认结果", file=sys.stderr)
    return ""


if __name__ == "__main__":
    # Windows 管道/控制台默认 GBK：emoji 会让 print 崩掉（工作其实已完成）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 自检：打印识别到的配置（不打印 key 明文）
    b, k, m = load_llm_config()
    print(f"base_url: {b}")
    print(f"api_key: {'SET(len=%d)' % len(k) if k else 'EMPTY'}")
    print(f"model: {m} → resolved: {resolve_api_model(m, b)}")
