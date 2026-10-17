# llm-wiki-copilot

> **让你的 AI Agent 帮你把碎片内容变成可调用的知识体系。**
> 本地运行 · 纯 Markdown · 零数据库依赖 · 数据永远是你自己的

你每天读到的好文章、看过的视频、收藏的工具，大多读完就散了。
这个项目做一件事：**把它们编译成结构化知识，再让你能用自然语言随时调用、深读、沉淀。**

不是另一个笔记软件 —— 它没有界面。它是**给 AI Agent 用的一套知识管理 SOP**：
Agent 读它里面的提示词指令，调用它里面的 Python 脚本，帮你完成从入库到内化的全流程。

```
视频/文章/PDF/截图
   ↓  ① 采集（可选集成）
   ↓  ② #ingest 编译：总结页 + 概念页 + 自动关联
   ↓  ③ #search / #ask 语义检索（本地向量库）
   ↓  ④ #start 逐段带读、#distill 深度复盘
   ↓  ⑤ 沉淀为可复用的知识网络
```

---

## 它是什么 / 不是什么

| ✅ 是 | ❌ 不是 |
|---|---|
| 给 Claude Code / Codex / Cursor / Gemini CLI 等 Agent 用的**指令包 + 脚本** | 独立 App（没有 GUI，不需要 GUI） |
| 本地优先：向量检索用本地 ONNX 模型，**零 API 也能跑** | 云服务（没有任何数据上传） |
| 纯 Markdown（Obsidian 可读，纯文本可迁移） | 又一个笔记软件（它不替代 Obsidian，是它的外挂大脑） |
| 对 AI Agent 的一种**用法约定**（12 个 `#指令`） | 通用的 AI 聊天客户端 |

**前置要求**：你已经在用某个能读写文件 + 执行终端命令的 AI Agent。
没有 Agent 的话，这套东西只是一堆 Markdown 和 Python 脚本。

---

## 30 秒安装

```bash
git clone https://github.com/<you>/llm-wiki-copilot.git
cd llm-wiki-copilot

# Windows
powershell -ExecutionPolicy Bypass -File install.ps1

# macOS / Linux
bash install.sh
```

安装器会自动：建虚拟环境 → 装依赖 → **问你 3 个问题** → 生成配置与知识库骨架 → 生成 Agent 入口文件。

首次约 1-3 分钟（含依赖下载）。向量模型（241 MB）首次检索时自动下载。

<details>
<summary>手动安装（不想跑脚本的话）</summary>

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt          # macOS / Linux

.venv/Scripts/python.exe setup.py     # 初始化向导
```

</details>

---

## 开始使用

安装完你会得到两个目录：

```
<引擎目录>/llm-wiki-copilot/     ← 代码与提示词（git 管理，只在你升级时变动）
<知识库目录>/                     ← 你的知识，用 Obsidian 打开这个
```

三步上手：

```bash
# 1. 把任意文章 / PDF / 截图丢进 <知识库>/00-INBOX/

# 2. 对你的 AI Agent 说：
#ingest

# 3. 开始深读
#start <标题>
```

---

## 12 个指令

| 指令 | 干什么 |
|---|---|
| `#ingest` | 批量编译入库：格式转换 → 分析 → 生成总结页/概念页 → 建关联 |
| `#search` | 全库混合检索（中文分词 + 向量语义 + 双链扩展 + 实体搭桥） |
| `#ask` | 知识库问答：先判断问题复杂度，再决定直接答还是先引导 |
| `#start` | 逐段带读：拆解 → 你提问 → 确认理解（**你主导，AI 不抛题**） |
| `#distill` | 深度复盘：思维导图 + 逻辑重组解读 + 对话增量 + 灵感沉淀 |
| `#bridge` | 织网：找出全库隐含关联 |
| `#lint` | 差异审查：5 级差异标注（冲突/视角/定义/时序/内部不一致） |
| `#evergreen` | 知识升华：多篇交叉 → 体系综述 |
| `#research` | 主动搜索补充：拆解 → 多平台搜索 → 筛选 → 入库 |
| `#stars` | 同步 GitHub 星标项目入库（可选） |
| `#index` | 向量索引管理（增量/全量/重建/统计） |
| `#archive` | 断舍离：标记归档，移出待处理 |

中文别名同样可用（`#入库` `#搜索` `#问` `#开始` `#复盘` …）。

触发方式就是**直接对 Agent 说**，它会读 `system/commands/<指令>.md` 按流程执行。

---

## ⚠️ 安装可自动，授权必须人工

这句要放在最显眼的地方，免得你装完才发现用不了：

| 事项 | 为什么无法自动化 |
|---|---|
| **平台登录态** | 扫码/授权属于你的账号，任何脚本都不能替你登录 |
| **飞书云端工作流** | 必须在飞书界面手工创建，API 不能代建 UI 配置 |
| **各类密钥** | LLM API key / GitHub token 只能由你提供 |

好消息：**核心链路（编译 → 检索 → 带读 → 沉淀）完全不需要这些**，
不配 LLM、不装采集层、不做任何登录，也能完整使用。

---

## 可选集成的真实代价

| 集成 | 装什么 | 体积/时间 |
|---|---|---|
| GitHub 星标同步 | 无需额外安装（脚本内置） | — |
| OpenCLI（8 平台搜索） | `npm i -g @jackwener/opencli` | 小，但需各平台扫码 |
| Chubby（B站/公众号/X/小红书抓取） | `git clone` + `bash setup.sh video` | 小到中 |
| 抖音转录 | `funasr` + `torch` + SenseVoice 模型 + ffmpeg | **2-4 GB / 10-30 分钟** |
| 飞书收集管道 | `lark-cli` + **自建多维表格与工作流** | 手工活最多 |

详见 [`integrations/README.md`](integrations/README.md)。

---

## 依赖与致谢

本项目的核心能力站在这些优秀开源项目肩上（**仓库内不含它们的代码**，通过 pip 或引导安装使用）：

| 组件 | 作者 | 许可 | 用途 |
|---|---|---|---|
| [zvec](https://github.com/alibaba/zvec) | Alibaba | Apache-2.0 | 本地向量库（含中文分词 FTS） |
| [fastembed](https://github.com/qdrant/fastembed) | Qdrant | Apache-2.0 | 本地 ONNX 向量化（零 API） |
| [markitdown](https://github.com/microsoft/markitdown) | Microsoft | MIT | 多格式 → Markdown |
| [jieba](https://github.com/fxsjy/jieba) | fxsjy | MIT | 中文分词 |
| [Chubby Skills](https://github.com/chubbyguan/chubbyskills) | chubbyguan | MIT | 平台抓取与转录（可选） |
| [OpenCLI](https://github.com/jackwener/opencli) | jackwener | Apache-2.0 | 多平台搜索（可选） |

方法论参考：概念抽离与矛盾标记的思路蒸馏自 Obsidian 第二大脑实践；编译模板的三轨分流参考了 Karpathy 的 LLM Wiki 思路。

---

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/quickstart.md`](docs/quickstart.md) | 完整使用指南（从装到用，含常见问题） |
| [`docs/architecture-flowcharts.md`](docs/architecture-flowcharts.md) | 架构与全链路流程图 |
| [`docs/pitfalls.md`](docs/pitfalls.md) | **踩坑合集** —— 建议先扫一遍，能省你几个小时 |
| [`integrations/README.md`](integrations/README.md) | 可选集成的安装与授权 |

---

## 设计取舍（为什么长这样）

- **引擎与数据分离** —— 引擎可 `git pull` 升级而不碰你的知识；你的知识库目录保持干净（Obsidian 里不会看到一堆提示词文件）
- **提示词即代码** —— 流程规则写在 Markdown 里，你可以直接改，不需要懂编程
- **可选依赖一律「有则用，无则跳过并告知」** —— 缺任何一项都不会报错，也不会静默失败
- **不 vendor 第三方** —— 不让仓库变成别人的代码仓库，也不让上游更新丢失

## License

MIT
