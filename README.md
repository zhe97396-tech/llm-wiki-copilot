# llm-wiki-copilot

**让你的 AI Agent 把碎片内容编译成可调用的知识体系。**

本地运行 · 纯 Markdown · 零数据库 · 零 API 也能检索 · 数据永远是你自己的

---

## 先说结果：它用起来是什么样

把一篇文章丢给它编译后，你可以直接问：

```
你  #search 怎么才能记得更牢

    → concepts/遗忘曲线        (相关度 0.0325)
      neighbors: 费曼学习法 · 间隔重复
      backend: zvec
```

它不是关键词匹配。检索走**中文分词 + 本地向量语义 + 双链邻居扩展 + 实体搭桥**，
而且**全部在本地跑**（ONNX 模型，241 MB，首次自动下载），不传任何数据出去。

`#ask` 会先判断问题复杂度：简单问题直接答，复杂问题先引导你自己想；
`#start` 逐段带读（你来提问，AI 不抛题）；`#distill` 做深度复盘。

---

## What —— 这是什么

它**不是**笔记软件，它没有界面。

它是**给 AI Agent 用的一套知识管理 SOP**：Agent 读它里面的提示词指令，
调用它里面的 Python 脚本，替你完成从入库到内化的全流程。

| ✅ 它是 | ❌ 它不是 |
|---|---|
| 给 Claude Code / Codex / Cursor / Gemini CLI 用的**指令包 + 脚本** | 独立 App（没有 GUI，也不需要） |
| 本地优先：向量检索用本地 ONNX，**零 API 也能跑** | 云服务（不上传任何数据） |
| 纯 Markdown（Obsidian 可读，纯文本可迁移） | 又一个笔记软件（它不替代 Obsidian，是它的外挂大脑） |
| 对 AI Agent 的一种**用法约定**（12 个 `#指令` + 原生斜杠命令） | 通用 AI 聊天客户端 |

> **前置条件**：你已经在用某个能读写文件、能执行终端命令的 AI Agent。
> 没有 Agent 的话，这套东西只是一堆 Markdown 和 Python 脚本。

---

## Why —— 为什么需要它

**痛点**：你每天读的好文章、看的视频、收藏的工具，绝大多数读完就散了。
收藏夹越堆越多，真要用的时候什么都找不到 —— 因为**收藏 ≠ 消化**，
而"消化"恰恰是最耗人力的那一步。

**它做的事**：把"消化"变成 Agent 的可重复流程。

```
视频 / 文章 / PDF / 截图 / 链接
   ↓  ① 采集（把链接发给 Agent 就够了）
   ↓  ② #ingest 编译：总结页 + 概念页 + 自动关联
   ↓  ③ #search / #ask 检索（本地向量库）
   ↓  ④ #start 逐段带读、#distill 深度复盘
   ↓  ⑤ 沉淀成一张会自己长大的知识网络
```

**和"让 AI 总结一下"的区别**：总结是一次性的，编译是**沉淀**——
生成的文件有结构、有双链、有索引，下次能被检索到、能被引用、能被更新。
第二次读同一主题时，Agent 会先告诉你"这个你已经有 3 篇相关笔记了"。

---

## Who —— 谁适合用

| 适合 | 不适合 |
|---|---|
| 已经在用 Claude Code / Codex / Cursor / Gemini CLI 的人 | 还没用过任何 AI Agent 的人（先去用一周再来） |
| 有长期积累习惯，笔记已经多到"搜不到"的人 | 只想要"一键总结"的人（那用现成 App 更快） |
| 在意数据主权，不想把笔记传到云端的人 | 希望开箱即用、不想碰终端的人 |
| 愿意读一遍文档、按自己习惯调提示词的人 | 期待装完就自动运转、零配置的人 |

**说实话**：这个项目要你投入一次配置（约 3 分钟），外加愿意理解它的 12 个指令。
它的回报率随你的积累量上升 —— 笔记越少，收益越不明显。

---

## When & Where —— 什么时候用、装在哪儿

**用它的典型时刻**：

- 读到一篇好文章，想让它变成"以后能查到的东西" → `#ingest`
- 想不起某个概念自己记过没有 → `#search` / `#ask`
- 想真正读懂一篇长文，而不是划几条线 → `#start`
- 读完想做一次彻底复盘、留下可复用结论 → `#distill`
- 发现笔记之间该有联系却没有 → `#bridge`

**装在哪儿**（引擎与数据分离）：

```
<引擎目录>/llm-wiki-copilot/     ← 代码与提示词（git 管理，只在升级时变动）
<知识库目录>/                     ← 你的知识（用 Obsidian 打开这个）
```

引擎可以随时 `git pull` 升级而**不碰你的知识**；知识库目录保持干净，
Obsidian 里不会看到一堆提示词文件。

---

## How —— 怎么开始

### 1. 安装（约 3 分钟）

```bash
git clone https://github.com/<you>/llm-wiki-copilot.git
cd llm-wiki-copilot

# Windows
powershell -ExecutionPolicy Bypass -File install.ps1

# macOS / Linux
bash install.sh
```

安装器会自动建虚拟环境、装依赖、**问你 3 个问题**（知识库放哪、关注什么主题）、
生成配置与知识库骨架、并生成各平台的 Agent 入口文件与命令。

<details>
<summary>手动安装（不想跑脚本）</summary>

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt          # macOS / Linux

.venv/Scripts/python.exe setup.py     # 初始化向导
```

</details>

### 2. 第一次成功（两分钟）

```bash
# ① 把任意文章 / PDF / 截图丢进 <知识库>/00-INBOX/
# ② 对你的 Agent 说：
#ingest

# ③ 然后问它：
#search <刚才那篇的主题>
```

看到检索结果里出现你刚丢进去的内容 —— 你就跑通了。

### 3. 然后就随手用

```
#ask 我之前记过的关于 XX 的观点有哪些？
#start <某篇标题>        → 带我精读这篇
#distill <某篇标题>      → 给这篇做深度复盘
```

---

## 12 个指令

| 指令 | 别名 | 干什么 |
|---|---|---|
| `#ingest` | `#入库` | 批量编译入库：格式转换 → 分析 → 生成总结页/概念页 → 建关联 |
| `#search` | `#搜索` | 全库混合检索（中文分词 + 向量语义 + 双链扩展 + 实体搭桥） |
| `#ask` | `#问` | 知识库问答：先判断复杂度，再决定直接答还是先引导 |
| `#start` | `#开始` | 逐段带读：拆解 → 你提问 → 确认理解（**你主导，AI 不抛题**） |
| `#distill` | `#复盘` | 深度复盘：思维导图 + 逻辑重组解读 + 对话增量 + 灵感沉淀 |
| `#bridge` | `#织网` | 织网：找出全库隐含关联 |
| `#lint` | `#纠错` | 差异审查：5 级标注（冲突/视角/定义/时序/内部不一致） |
| `#evergreen` | `#常青` | 知识升华：多篇交叉 → 体系综述 |
| `#research` | `#调研` | 主动搜索补充：拆解 → 多平台搜索 → 筛选 → 入库 |
| `#stars` | `#星标` | 同步 GitHub 星标项目入库（可选） |
| `#index` | `#索引` | 向量索引管理（增量/全量/重建/统计） |
| `#archive` | `#冷宫` | 断舍离：标记归档，移出待处理 |

### 斜杠命令：各平台原生入口

除了 `#` 触发，本项目还**为各平台生成了原生命令**，两者完全等价：

| 平台 | 命令位置 | 怎么调 |
|---|---|---|
| **Claude Code** | `<知识库>/.claude/commands/<name>.md` | 输入 `/` 会看到 `ingest`、`search`、`ask` … |
| **Gemini CLI** | `<知识库>/.gemini/commands/<name>.toml` | 同样 `/ingest` |
| **Codex CLI** | —— | 自定义命令已在 0.117.0 移除，用 `#指令` 或 skills |
| **其他 Agent** | —— | 用 `#指令`，任何能读文件的 Agent 都认 |

命令文件只是**转发层**：正文指向 `system/commands/<name>.md`，
真正的步骤定义只有一份，不会出现两份真相。

重新生成（升级引擎后跑一次即可）：

```bash
python scripts/build_adapters.py
```

> 提示词层的流程规则写在 Markdown 里，**你可以直接改**，不需要懂编程。

---

## 采集：把链接发给它就够了

默认情况下你**不需要装任何采集工具**：

```
入库这个 https://example.com/some-article
```

Agent 会自己抓正文 → 转 Markdown → 编译 → 建索引。PDF、截图、复制的文字同样处理。

**一次给多个链接也行** —— 一股脑丢过去：

```
把这几个入库
https://...
https://...
https://...
```

视频类链接（如抖音）会走转录脚本，**多条会自动用批量模式**（模型只加载一次，比逐条快很多，
且按视频 ID 记账去重、中断可续跑）。

只有当你要**批量 / 自动化**时，才需要选装下面的集成
（详见 [`integrations/README.md`](integrations/README.md)，每条命令都可以让 Agent 代劳）：

| 集成 | 什么时候才需要 | 体积 |
|---|---|---|
| GitHub 星标同步 | 想把 GitHub 星标批量入库 | 无需安装（脚本内置） |
| Chubby（B站/公众号/X/小红书抓取） | 要批量抓平台内容或视频字幕 | 中 |
| OpenCLI（8 平台搜索） | 想让 Agent 自己上平台调研 | 小 |
| 抖音转录 | 要批量把抖音视频转文字稿 | **2-4 GB** |

### ⚠️ 安装可自动，授权必须人工

这三件事**永远无法**由脚本完成，因为它们是"你的账号"：

| 事项 | 为什么 |
|---|---|
| 平台登录态 | 扫码/授权属于你的账号，任何脚本都不能替你登录 |
| 云端工作流配置 | 例如飞书多维表格与工作流，必须在对方界面手工创建 |
| 各类密钥 | LLM API key / GitHub token 只能由你提供 |

好消息：**核心链路（编译 → 检索 → 带读 → 沉淀）完全不需要这些。**

---

## 密钥与安全

**你的密钥不会被提交、不会被打印，而且不配也能用。**

| 措施 | 你可以自己验证 |
|---|---|
| `config.yaml` / `.env` / `*.local` 都在 `.gitignore` 里，**永远不会进 git** | `cat .gitignore` |
| 配置模板里只有空占位符，没有真实值 | `grep -i "key\|token\|cookie" config.example.yaml` |
| **代码从不打印密钥值** —— 自检只显示「是否设置 + 长度」 | `python scripts/llm_api.py` → `api_key: SET(len=51)` |
| 生成的入口文件与斜杠命令文件里不含密钥 | `grep -i api_key CLAUDE.md AGENTS.md GEMINI.md` |
| 缺密钥时优雅跳过并明确告知，不报错、不静默失败 | 不配任何 key 直接跑 `#ingest` |

**布局上的保护**：密钥放**引擎目录**的 `config.yaml`（或环境变量），你的知识在**数据目录**。
所以你分享或备份知识库目录时，**里面没有密钥**。

一旦怀疑泄露，**第一优先是去服务商后台吊销重签** —— 改 git 历史救不了已经泄露出去的那一份。

完整的配置方式、自查清单、共享/服务器环境建议：[`docs/secrets.md`](docs/secrets.md)

---

## 设计取舍（为什么长这样）

- **引擎与数据分离** —— 引擎可升级而不碰你的知识
- **提示词即代码** —— 流程规则写在 Markdown 里，你能直接改
- **可选依赖一律「有则用，无则跳过并告知」** —— 缺任何一项都不报错、也不静默失败
- **不打包第三方代码** —— 仓库零外部代码，上游更新不丢失，边界清晰
- **本地优先** —— 检索不依赖任何云服务，断开网络也能用

---

## 依赖与致谢

核心能力站在这些开源项目肩上（**仓库内不含它们的代码**，通过 pip 或引导安装使用）：

| 组件 | 作者 | 许可 | 用途 |
|---|---|---|---|
| [zvec](https://github.com/alibaba/zvec) | Alibaba | Apache-2.0 | 本地向量库（含中文分词 FTS） |
| [fastembed](https://github.com/qdrant/fastembed) | Qdrant | Apache-2.0 | 本地 ONNX 向量化（零 API） |
| [markitdown](https://github.com/microsoft/markitdown) | Microsoft | MIT | 多格式 → Markdown |
| [jieba](https://github.com/fxsjy/jieba) | fxsjy | MIT | 中文分词 |
| [PyYAML](https://github.com/yaml/pyyaml) | YAML | MIT | 配置解析 |
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) | Ray Smith 等 | Apache-2.0 | 图片文字识别（可选） |
| [Chubby Skills](https://github.com/chubbyguan/chubbyskills) | chubbyguan | MIT | 平台抓取与转录（可选） |
| [OpenCLI](https://github.com/jackwener/opencli) | jackwener | Apache-2.0 | 多平台搜索（可选） |

方法论参考：概念抽离与矛盾标记的思路蒸馏自 Obsidian 第二大脑实践；
三轨编译模板的分流思路受 Karpathy 的 LLM Wiki 讨论启发。

---

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/quickstart.md`](docs/quickstart.md) | 完整使用指南（从装到用，含常见问题） |
| [`docs/architecture-flowcharts.md`](docs/architecture-flowcharts.md) | 架构与全链路流程图 |
| [`docs/pitfalls.md`](docs/pitfalls.md) | **踩坑合集 52 条** —— 建议先扫一遍，能省你几个小时 |
| [`docs/secrets.md`](docs/secrets.md) | **密钥与安全** —— 密钥怎么配、本项目如何处理、泄露自查清单 |
| [`integrations/README.md`](integrations/README.md) | 可选集成的安装与授权 |
| [`demo/`](demo/) | 可跑的示例数据（含检索验证） |

---

## License

MIT
