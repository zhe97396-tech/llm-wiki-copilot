# Changelog

## v0.2.1 — 2026-11-07

加了一层"改完检索到底有没有坏"的护栏。

### 新增

- **`scripts/ci_check.py`**：一条命令做 7 类自检
  1. 全量语法编译（`scripts/` + `integrations/mcp/`）
  2. 分块模块可导入、`split_page` 在合成页面上确实切出多块且带 `页面 > 小节` 前缀
  3. `search.py` 结构（融合函数存在、目录去重参数没被改回 2/0.5）
  4. MCP 暴露 6 个工具且 `related` 已注册
  5. 分块索引逐块哈希新鲜
  6. 真实 MCP 协议往返（`search` 有结果、`related` 有兄弟页）
  7. 回归台 hit@3 不低于阈值（默认 50%）
- **GitHub Actions（`.github/workflows/ci.yml`）**：两个 job
  - `static`：不装 zvec / fastembed，10 秒内完成语法与注册表检查
  - `retrieval`：Ubuntu 22.04 装 `zvec==0.5.1` + fastembed，用 `demo/` 语料真跑
    embed → embed_chunks → entity_index → 自检 → **索引自愈回归**（删库后 `--incremental` 必须自动全量重建）
    → 关闭分块后的降级路径（`LLM_WIKI_CHUNK_SEARCH=off` 不得报错）
  - fastembed 模型（241MB）走 actions/cache，避免每次下载

### 说明

- 本地也能跑同一条命令：`PYTHONPATH="" python scripts/ci_check.py --data <知识库> --bench-cases my-cases.json`
- 回归台阈值默认 50% 是**防明显退化**，不是质量标准；请不要为了让 CI 变绿而调低它

---

## v0.2.0 — 2026-11-07

检索层重写。这一版的核心结论：**检索不准，多半不是模型不够强，而是"整页一个向量"这个结构本身在稀释证据。**

### 新增

- **分块索引（第二召回源）**：`scripts/embed_chunks.py` + `scripts/zvec_chunk_config.py`
  - 每页按 `##`/`###` 切块，块文本前置 `页面标题 > 小节名` 作为最廉价的 contextual retrieval
  - 独立数据库 `.llm-wiki/zvec_db_chunks/`，与整页索引物理隔离，出问题互不影响
  - 块级 SHA-256 增量：只重嵌入变化的小节，并清理已消失的小节
  - 索引被删/损坏时自愈：检测到"缓存有 N 块、库里只剩不到一半"会自动转为全量重建
  - `LLM_WIKI_CHUNK_SEARCH=off` 可一键退回原链路；索引缺失或报错时检索自动降级，不报错
- **`search` 双路 RRF 融合**：分块结果与整页结果同尺度相加（两边 `rank_constant=60`），
  同时被两路命中的页面排名上升；分块命中的页面 `snippet` 直接返回**命中段落**而非页面开头
- **`scripts/bench_search.py`**：读问题集 JSON 的回归台（hit@1 / hit@3 / MRR），自带 `bench/cases.example.json` 模板
- **`health.py --check zvec_chunks`**：逐块哈希比对分块索引是否落后，落后时直接给出要跑的命令
- **编译规则新增「同义词入口」**：页面正文开头写 `> **同义词**：口语说法、场景提问、同义专业词、常见简称`。
  页面标题是内行词、用户提问是外行词，没有词桥就搜不到
- **MCP 新增 `related` 工具**：给定页面 id，返回共享核心实体的同簇兄弟页（IDF 加权抑制"精准定位"这类枢纽概念，
  避免把只沾大方向的页面也混进来）。只读本地 sqlite，不加载模型，约 0.01s / 8 条约 1.2k token

### 调整

- 同目录去重参数 `DIVERSITY_MAX_SAME_DIR` 2 → 4、`DIVERSITY_PENALTY` 0.5 → 0.8：
  原参数会把"一个方向多篇"的兄弟页压到无关页后面（实测同一 query top20 相关条数 7 → 11）
- 分块索引维度改为读取 `zvec_config.EMBEDDING_DIMENSION`，不再写死 384

### 实测（272 篇自有知识库，20 条真实提问）

| 阶段 | hit@1 | hit@3 | MRR |
|---|---|---|---|
| 基线（整页单向量 + 混合检索） | 35% | 75% | 0.544 |
| + 分块索引融合 | 55% | 80% | 0.654 |
| + 页面同义词入口 | **65%** | **95%** | **0.767** |

单次检索耗时 2.3–2.9s（与之前持平），分块检索额外开销 0–0.9s。

### 已知边界

- 分块索引需要随页面更新：`#ingest` 已内置该步骤；手动改页面后要补跑 `embed_chunks.py --incremental`
- `ask` 的 coverage 偏乐观（实测存在"coverage=1.0 但前 3 名无对口页"的情况），判断"库里到底有没有"请结合 `search` 结果
- `related` 的簇粒度取决于实体索引质量：概念抽取稀疏的页面，同簇结果会退化
- `bench_search.py` 不内置问题集（内置就会变成"用自己的题刷自己"）：请自备 15–20 条真实提问

---

## v0.1.0

首个公开版本：`#ingest` 编译链路、Zvec 混合检索（jieba FTS + 向量 + RRF）、
概念页自动蒸馏、双链建链规则、`#search` / `#ask` / `#start` / `#distill` 等 12 个指令、
只读 MCP（search / fetch / list_concepts / stats / ask）、健康检查与入库收尾体检。
