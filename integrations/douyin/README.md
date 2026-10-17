# 抖音视频转录集成

> 把抖音链接变成可入库的 Markdown 文字稿。**这是本项目自制的脚本**（不是第三方），MIT 许可。

## 它做什么

```
抖音链接
  → 取视频元信息（标题/作者/时长，多级降级：分享页 → playwright 渲染拦截 API）
  → 下载音频（ffmpeg 抽 WAV）
  → SenseVoice 转录
  → 规则纠错词表（确定性兜底）
  → LLM 分段校对（可选，未配 LLM 时跳过）
  → 生成带 frontmatter 的 Markdown → 落 00-INBOX/
```

## 依赖（这一步比较重，做好心理准备）

| 依赖 | 说明 |
|---|---|
| Python 3.10+ | 建议**独立 venv**（与知识库引擎分开） |
| `funasr` + `torch` + `torchaudio` + `modelscope` | 约 2-4 GB（CPU 版 torch 也不小） |
| SenseVoice 模型 | 首次运行自动从 ModelScope 下载（数百 MB） |
| `playwright` | 渲染抖音页面以拦截详情 API；需系统装有 Edge/Chrome |
| `ffmpeg` | 音频抽取（需在 PATH 中） |

```bash
# 建独立 ASR 环境（位置自选，示例用 <ASR_ENV>）
python -m venv <ASR_ENV>
<ASR_ENV>/Scripts/python.exe -m pip install funasr torch torchaudio modelscope playwright
<ASR_ENV>/Scripts/python.exe -m playwright install msedge   # 或用系统已装的 Edge/Chrome
```

装好后写进 `config.yaml`：

```yaml
integrations:
  douyin:
    enabled: true
    python: "<ASR_ENV>/Scripts/python.exe"
    transcribe_script: "<引擎目录>/integrations/douyin/douyin_transcribe.py"
    download_script: ""      # 可选：用 Chubby 的下载脚本替代内置下载
```

## 用法

```bash
# 单条
<asr_python> integrations/douyin/douyin_transcribe.py "https://v.douyin.com/xxxxx/"

# 批量（模型只加载一次，多条复用）
<asr_python> integrations/douyin/douyin_batch.py --file 链接清单.txt

# 校对失败的稿子补跑校对
<asr_python> integrations/douyin/recheck_correct.py "<md 路径>"
```

产物默认落到 `<data_root>/00-INBOX/`，随后用 `#ingest` 编译入库。

## 防重账本

脚本内置账本（`<data_root>/.llm-wiki/douyin_ledger.db`，按 `video_id` 索引，O(1)）：
**同一个视频不会重复转录**。这条链路是自动的，你不需要手动查重。

> ⚠️ 抖音短链（`v.douyin.com/xxx`）与长链（`douyin.com/video/<id>`）字符串匹配不上，
> 所以**必须按 `video_id` 判重** —— 账本已经处理了这件事。

## 已知脆弱点（抖音改版史）

抖音页面与接口经常改版，抓取链路需要跟着修：

| 时期 | 主路径 |
|---|---|
| 早期 | iesdouyin 分享页的 `_ROUTER_DATA` |
| 2026-08 起 | 该路径失效 → 改为 playwright 渲染 `www.douyin.com`，拦截 `aweme/v1/web/aweme/detail` API |

**症状**：报「playwright 未能捕获 aweme detail API 响应」= 抖音又改版了，需要重新适配这一处。

## 为什么不用第三方方案

其他「抖音转录」工具通常只做下载或只做 ASR。本项目这条链路多了两件事：
**规则纠错词表**（确定性兜底，专治谐音错字）和 **LLM 分段校对**（可选，长文质量提升明显）。
