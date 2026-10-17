# 可选集成

> **核心链路不需要这里的任何一项。** 编译 → 检索 → 带读 → 沉淀，全部零集成可用。
> 只有你想让「外部内容自动进来」时，才需要往下看。

---

## 最重要的一件事：发链接就能入库

**大多数情况下你不需要装任何采集工具。**

把链接丢给你的 Agent，说一句：

```
入库这个 https://example.com/some-article
```

Agent 会自己做完：抓正文 → 转 Markdown → 编译成总结页与概念页 → 建索引。
之后 `#search` / `#ask` 就能搜到它。PDF、截图、整篇复制过来的文字，同样处理。

**这是默认的采集入口，够用就别往下折腾。**

---

## 什么时候才需要下面的集成

只有「批量」和「自动化」才需要。对照下表，不符合就别装：

| 集成 | 满足什么条件才装 | 装什么 | 体积 | 需要你手工做的 |
|---|---|---|---|---|
| **github-stars** | 想把 GitHub 星标批量同步入库 | 无需安装（脚本已内置） | — | 提供 GitHub 用户名 |
| **chubby** | 要**批量**抓 B站/公众号/X/小红书，或要视频字幕 | 第三方仓库（MIT） | 中 | 部分平台登录态 |
| **opencli** | 想让 Agent 自己上 **8 个平台搜索**调研 | 第三方 npm 包 | 小 | 各平台扫码登录 |
| **douyin** | 要**批量**把抖音视频转成文字稿 | funasr + torch + ffmpeg | **2-4 GB** | 装 ffmpeg |
| *feishu* | *（规划中，暂未提供）* | — | — | — |

---

## 安装方式

> **下面每一条都可以直接让你的 Agent 代劳。**
> 把这一节整段贴给它，说「照着装」，它会执行命令并把结果告诉你；
> 只有最后一步「扫码登录 / 填密钥」必须你本人做。

### github-stars（无需安装）

脚本内置，只要在 `config.yaml` 里填上你的用户名即可：

```yaml
integrations:
  github_stars:
    enabled: true
    username: "你的GitHub用户名"
```

之后 `#stars` 可同步。公开仓库不需要 token（有速率限制）；要更高额度就配 `GITHUB_TOKEN`。

### chubby（B站/公众号/X/小红书 抓取与转录）

- **来源**：<https://github.com/chubbyguan/chubbyskills>（MIT，作者 chubbyguan）
- **装法**：

```bash
git clone https://github.com/chubbyguan/chubbyskills.git
cd chubbyskills && bash setup.sh video
# 分档可选：light / video / podcast / wechat / all / doctor
```

- **装到哪**：你选一个目录（建议与引擎目录同级，例如 `D:/Tools/chubbyskills`）。
- **配置**：装好后把路径写进 `config.yaml`：

```yaml
integrations:
  chubby:
    enabled: true
    path: "D:/Tools/chubbyskills"
```

- **需要你做的**：部分平台要登录态（`XHS_COOKIE` 等），装完按它的提示登录一次。

### opencli（8 平台搜索）

- **来源**：npm 包 `@jackwener/opencli`（Apache-2.0，作者 jackwener）
- **装法**：

```bash
npm i -g @jackwener/opencli
opencli --version          # 验证
```

- **装到哪**：npm 全局目录（`npm root -g` 可查）。
- **配置**：

```yaml
integrations:
  opencli:
    enabled: true
```

- **需要你做的**：首次使用时逐平台扫码登录（登录态存在 opencli 自己的目录里）。

### douyin（抖音视频 → 文字稿）

- **装什么**：`funasr` + `torch` + `SenseVoice` 模型 + `ffmpeg`（约 2-4 GB）
- **装法**（建独立环境，别污染主环境）：

```bash
python -m venv <ASR_ENV>
<ASR_ENV>/Scripts/python.exe -m pip install funasr torch torchaudio modelscope playwright
<ASR_ENV>/Scripts/python.exe -m playwright install msedge
```

- **本项目提供的脚本**：`integrations/douyin/`（转录 + 批量 + 纠错校对，共 4 个）
- **配置**：

```yaml
integrations:
  douyin:
    enabled: true
    python: "<ASR_ENV>/Scripts/python.exe"
    transcribe_script: "<引擎目录>/integrations/douyin/douyin_transcribe.py"
```

- **需要你做的**：`ffmpeg` 要在 PATH 里。

---

## 依赖与致谢

本项目的核心能力站在这些开源项目肩上。**仓库内不含它们的代码**：

| 组件 | 作者 | 许可 | 我们怎么用它 |
|---|---|---|---|
| [zvec](https://github.com/alibaba/zvec) | Alibaba | Apache-2.0 | pip 依赖 · 本地向量库（中文分词 FTS） |
| [fastembed](https://github.com/qdrant/fastembed) | Qdrant | Apache-2.0 | pip 依赖 · 本地 ONNX 向量化 |
| [markitdown](https://github.com/microsoft/markitdown) | Microsoft | MIT | pip 依赖 · 多格式 → Markdown |
| [jieba](https://github.com/fxsjy/jieba) | fxsjy | MIT | pip 依赖 · 中文分词 |
| [PyYAML](https://github.com/yaml/pyyaml) | YAML | MIT | pip 依赖 · 配置解析 |
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) | Ray Smith 等 | Apache-2.0 | 外部程序 · 图片文字识别 |
| [Chubby Skills](https://github.com/chubbyguan/chubbyskills) | chubbyguan | MIT | 引导安装 · 平台抓取（可选） |
| [OpenCLI](https://github.com/jackwener/opencli) | jackwener | Apache-2.0 | 引导安装 · 平台搜索（可选） |

> 如果你在自己的仓库里也用到它们，建议同样列出 —— 这是开源社区的基本礼貌。

---

## 三个永远无法自动化的事

| 事项 | 为什么 |
|---|---|
| **平台登录态** | 扫码/授权属于你的账号，任何脚本都不能替你登录 |
| **云端工作流配置** | 例如飞书多维表格与工作流，必须在对方界面手工创建 |
| **各类密钥** | LLM API key / GitHub token 只能由你提供 |

好消息：**核心链路（编译 → 检索 → 带读 → 沉淀）完全不需要这些**。
不配 LLM、不装任何采集工具、不做任何登录，也能完整使用。
