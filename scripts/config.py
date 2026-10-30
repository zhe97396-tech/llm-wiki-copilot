#!/usr/bin/env python3
"""统一配置加载器（llm-wiki-copilot）

优先级：环境变量 > config.yaml > 内置默认
本模块让所有脚本**零硬编码路径**，安装向导生成的 config.yaml 是唯一配置源。

config.yaml 位置：<引擎根>/config.yaml
可用环境变量 LLM_WIKI_CONFIG 指向其他位置。

用法：
    from config import get, data_root, get_path
    username = get("integrations.github_stars.username")
    root = data_root()
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # 提示用项目自带 venv
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("LLM_WIKI_CONFIG") or (REPO_ROOT / "config.yaml"))

_cache: dict | None = None


def load_config(force: bool = False) -> dict:
    """读取 config.yaml（带缓存）。缺失或解析失败时返回空 dict 并打印告警。"""
    global _cache
    if _cache is not None and not force:
        return _cache

    data: dict = {}
    if CONFIG_PATH.exists():
        if yaml is None:
            print(
                f"[config] ⚠ 缺少 PyYAML，无法读取 {CONFIG_PATH}\n"
                f"[config]   请用项目自带解释器运行："
                f".venv/Scripts/python.exe（Windows）或 .venv/bin/python（macOS/Linux）",
                file=sys.stderr,
            )
        else:
            try:
                data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            except Exception as e:
                print(f"[config] ⚠ config.yaml 解析失败：{e}", file=sys.stderr)
                data = {}
    elif not os.environ.get("LLM_WIKI_CONFIG"):
        # 没配置也能跑（全部走默认值），只在需要时静默
        pass

    _cache = data if isinstance(data, dict) else {}
    return _cache


def get(dotted: str, default=None):
    """按点号路径取值，空字符串视为未设置。例：get("llm.model")"""
    cur = load_config()
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    if cur is None or cur == "":
        return default
    return cur


def get_env_or_cfg(env_name: str, dotted: str, default=None):
    """环境变量优先于 config.yaml。"""
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    return get(dotted, default)


def data_root() -> Path:
    """知识库数据目录：config.data_root > 环境变量 LLM_WIKI_DATA > ./vault"""
    v = get("data_root") or os.environ.get("LLM_WIKI_DATA", "")
    p = Path(v).expanduser() if v else (REPO_ROOT / "vault")
    return p.resolve()


def get_path(dotted: str, default: Path | None = None) -> Path | None:
    """取路径型配置，自动展开 ~ 与环境变量。"""
    v = get(dotted)
    if not v:
        return default
    return Path(os.path.expandvars(str(v))).expanduser()


def is_enabled(dotted: str) -> bool:
    """判断集成是否启用：get("integrations.douyin.enabled")"""
    return bool(get(dotted, False))


def self_test() -> None:
    print("=== config.py 自检 ===")
    print(f"引擎根:   {REPO_ROOT}")
    print(f"配置文件: {CONFIG_PATH}  {'存在' if CONFIG_PATH.exists() else '不存在（走默认值）'}")
    print(f"PyYAML:   {'OK' if yaml else '缺失（请用项目 venv 运行）'}")
    print(f"数据目录: {data_root()}")
    print(f"分类池:   {get('categories', ['(默认)'])}")
    print(f"LLM:      base_url={get('llm.base_url', '(未配置)')} model={get('llm.model', '(未配置)')}")
    for name in ("github_stars", "douyin", "chubby", "opencli"):
        print(f"  集成 {name}: {'启用' if is_enabled(f'integrations.{name}.enabled') else '关闭'}")


if __name__ == "__main__":
    # Windows 管道/控制台默认 GBK：emoji 会让 print 崩掉（工作其实已完成）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    self_test()
