---
name: llm-wiki-copilot
description: "Use when the user triggers #ingest / #search / #ask / #start / #distill / #bridge / #lint / #evergreen / #research / #stars / #index / #archive, or references a local LLM Wiki / 知识副驾 knowledge base. 12 commands driving a local-first knowledge pipeline (compile → retrieve → read-along → distill) with zero-API RAG."
version: 0.1.0
license: MIT
metadata:
  tags: [knowledge-base, rag, markdown, local-first, obsidian]
---

# LLM Wiki Copilot — 知识副驾

**引擎位置**：本 skill 所在仓库根目录
**流程定义**：`system/commands/[指令名].md` —— 执行前**必须**读取对应文件，不要凭本 skill 的记忆执行
**数据位置**：由 `config.yaml` 的 `data_root` 指定（**不是**引擎目录）

---

## 一、指令触发协议

当用户输入包含 `#指令` 或中文别名时，这是**系统级命令调用，不是闲聊**：

1. 停止自由发挥
2. 读取 `system/SKILL.md`（指令索引）
3. 读取 `system/commands/[指令名].md`（详细步骤）
4. 回复第一行打印：`[⚡系统拦截：正在严格执行 #xxx 流程...]`

**绝对红线**：
1. **锚定原文** —— 回答 100% 建立在文档上下文上；知识库没有的，明确说"未收录"
2. **拒绝文学评论** —— 解读知识的现实意义，禁止分析"写作手法/叙事结构"
3. **克制输出** —— 精简回答，用问题引导用户自己得出结论

## 二、12 个指令

| 指令 | 别名 | 干什么 |
|---|---|---|
| `#ingest` | `#入库` | 批量编译入库：转换 → 分析 → 总结页/概念页 → 建关联 → 全局更新 |
| `#search` | `#搜索` | 全库混合检索（中文分词 FTS + 向量 + RRF + 双链扩展 + 实体搭桥） |
| `#ask` | `#问` | 知识库问答：先判复杂度，再决定直接答还是先引导 |
| `#start` | `#开始` | 逐段带读（**用户主导答疑，AI 不主动抛题**） |
| `#distill` | `#复盘` | 深度复盘：思维导图 + 逻辑重组 + 对话增量 + 灵感沉淀 |
| `#bridge` | `#织网` | 发现全库隐含关联 |
| `#lint` | `#纠错` | 5 级差异标注（冲突/视角/定义/时序/内部不一致） |
| `#evergreen` | `#常青` | 知识升华：多篇交叉 → 体系综述 |
| `#research` | `#调研` | 主动搜索补充：拆解 → 搜索 → 筛选 → 入库 |
| `#stars` | `#星标` | GitHub 星标同步入库（可选） |
| `#index` | `#索引` | 向量索引管理（增量/全量/重建/统计） |
| `#archive` | `#冷宫` | 标记归档，移出待处理 |

## 三、解释器与路径约定（用错会静默出错）

- **统一用项目 venv**：`.venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（macOS/Linux）
- **保留 `PYTHONPATH=""` 前缀**：某些宿主会注入自己的 venv 覆盖项目依赖（表现为 `No module named 'zvec'`）
- **路径相对 data_root**：`system/` 里出现的 `wiki/`、`raw/`、`00-INBOX/` 都相对 `config.yaml` 的 `data_root`
- **自检**：
  - `PYTHONPATH="" .venv/Scripts/python.exe scripts/config.py` → 引擎根 / 数据目录 / 集成开关
  - `PYTHONPATH="" .venv/Scripts/python.exe scripts/llm_api.py` → LLM 配置是否识别
  - `search.py` 返回的 `backend` 字段：`zvec` 正常 / `bigram` 说明语义检索降级

## 四、执行时必须守住的约定

| 约定 | 要点 |
|---|---|
| **双链铁律** | 只有 5 种关系才建链：补充 / 推翻矛盾 / 对比 / 时序演化 / 同主题互证。相似度分数只是召回工具，**不是**建链判据 |
| **概念判定** | 三条件同时满足才算概念：抽象性 + 可复用性 + 可定义性。工具名/产品名/人名不建概念页；一篇文章可以零概念 |
| **模板三轨** | 先看内容形态（工具/产物 > 观点/知识），再看篇幅。工具/清单/SOP 用模板 C，长文/逐字稿用 B，短文用 A |
| **去重层** | 比对 `raw/` 里源文件的 `source:` 字段，**不是** `wiki/`（编译产物的 source 是平台名） |
| **YAML 位置** | frontmatter 必须在文件**第一行**，不能包在 ```yaml 里 |
| **孤儿页** | 是孤儿就是孤儿，不强行补链；但规范链缺失必补（概念页须有"来源：宿主页"） |
| **可选依赖** | 一律"有则用，无则跳过并告知"，绝不硬调用 |

## 五、工具与脚本路由

| 任务 | 用什么 |
|---|---|
| 编译入库 | `scripts/convert.py --inbox`（多格式 → Markdown）+ 按 `system/PROMPT.md` 九步走 |
| 建索引 | `scripts/embed.py`（`--incremental` / `--file <绝对路径>` / `--rebuild` / `--stats`） |
| 检索 | `scripts/search.py "查询" --top N`（可 `--exclude-type`、`--check-dup`、`--similar`） |
| 问答 | `scripts/ask.py "问题"`（内含检索 + 复杂度判断 + 覆盖度评分） |
| 健康检查 | `scripts/health.py`（纯确定性，零 LLM；`--check <名字>` 单查） |
| 实体索引 | `scripts/entity_index.py --rebuild / --stats / --file <绝对路径>` |
| 带读前过滤 | `scripts/check_distilled.py --check "<标题>"`（exit 1 = 已沉淀，禁止重复带读） |
| 生成入口文件 | `scripts/build_adapters.py`（改 `system/SKILL.md` 后必须重跑） |

## 六、踩坑速查

完整 52 条见 `docs/pitfalls.md`。最高频的五个：

1. **检索像关键词匹配** → 查 `backend` 字段，`bigram` 说明 fastembed 没装上
2. **LLM 步骤集体失败** → 解释器缺 PyYAML（用项目 venv）
3. **`No module named 'zvec'`** → `PYTHONPATH=""` + 项目 venv
4. **文档加了但搜不到** → 没建索引，跑 `#index`
5. **双链乱** → 回顾"双链铁律"，别用分数当判据
