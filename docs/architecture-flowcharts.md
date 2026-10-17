# 流程图与架构图: LLM Wiki 能力升级

>
> ⚠️ **维护提醒**：wiki 项目有增删或调整时，必须同步更新以下两个文档：
> - `upgrade-todo/quickstart.md` — 使用指南（指令表、场景示例、FAQ）
> - `upgrade-todo/flowcharts.md` — 流程图与架构图（编译流程、搜索流程、脚本层）

---

## 一、文章状态流转图

一篇文章从"放进知识库"到"完全内化"的完整生命周期。

```mermaid
flowchart LR
    INBOX(["📥 00-INBOX"]) --> Compiled(["⚙️ Compiled<br/>已编译"])
    Compiled -->|"用户 #start"| Reading(["📖 Reading<br/>正在带读"])
    Reading -->|"用户 #复盘<br/>深度解读版→conversations/<br/>待深挖话题→wiki/ideas.md"| Distilled(["💎 Distilled<br/>已沉淀"])

    Compiled -->|"用户 #archive"| Archived(["🗑️ Archived<br/>已归档"])
    Reading -->|"用户 #archive"| Archived

    Distilled -->|"用户 #evergreen"| Evergreen(["🌲 Evergreen<br/>常青综述"])

    Compiled -.- note1["💡 大部分文章停在这里<br/>安静待着，等你哪天想看"]
    Reading -.- note2["💡 只有你主动触发<br/>才会进入深度分析"]

    style Compiled fill:#4CAF50,color:#fff
    style Reading fill:#2196F3,color:#fff
    style Distilled fill:#9C27B0,color:#fff
    style Archived fill:#9E9E9E,color:#fff
    style Evergreen fill:#FF9800,color:#fff
    style note1 fill:#FFF9C4,color:#333,stroke-dasharray: 5 5
    style note2 fill:#FFF9C4,color:#333,stroke-dasharray: 5 5
```

**人话版**：

```
放文章进来 → #ingest 编译（自动格式转换+生成总结+RAG索引+去重检测）（状态：compiled）
                │
                ├── 想深度看 → #start（自动推荐相似文档）→ 带读 → #复盘 → 深度解读版写入conversations/（已纳入Zvec索引）+ 待深挖话题五步流程（原料→筛选→验证→归类→进池）→ 沉淀完毕（distilled）
                │                                                              │
                │                                                              └── 可选 → #evergreen → 常青综述
                ├── 不想看 → #archive → 归档（archived）
                │
                └── 暂时不管 → 安静待着，不打扰你
```

---

## 二、整体架构图

改造后的系统长什么样，各部分怎么配合。

```mermaid
graph TB
    subgraph 摄入层["📥 摄入层（文章怎么进来）"]
        A1["手动放入 00-INBOX/"]
        A2["Obsidian Web Clipper"]
        A3["#research 网络搜索"]
        A4["scripts/convert.py<br/>MarkItDown: PDF/DOCX/PPTX/XLSX/EPUB/HTML/CSV/图片OCR"]
    end

    subgraph 编译层["⚙️ 编译层（#ingest 触发）"]
        B0["Step 1: 前置<br/>convert.py 格式转换+英文翻译<br/>读取 index.md + overview.md + purpose.md"]
        B1["Step 2: 分析原文<br/>和已有内容的关系+矛盾检测"]
        B2["Step 3: 生成总结+概念<br/>重写已有页面+标记矛盾"]
        B2_5["Step 3.5: 实体索引写入<br/>entity_index.py → entity_index.db（零LLM）"]
        B2_6["Step 4: RAG 索引<br/>embed.py → Zvec（FTS+向量）"]
        B2_7["Step 5: 语义去重<br/>新文档是否与已有文档重复"]
        B2_8["Step 6: 增量关联与进化<br/>5类关系+无关系跳过（2026-08-02修订）<br/>补充→自动融入正文+建双链<br/>推翻→标记矛盾<br/>对比/时序→注明关系+双向建链<br/>同主题互证→双向建链<br/>无关系/主题擦边→不建链静默跳过<br/>质量兜底: #lint每周自动审查"]
        B3["Step 7: 全局更新<br/>overview.md + index.md"]
        B4["Step 8: 弱关联提醒（增量织网）<br/>发现隐含关联+Zvec语义匹配建议建链"]
        B5["Step 9: 分类路由校验<br/>health.py 自动修正 YAML category"]
        B0 --> B1 --> B2 --> B2_5 --> B2_6 --> B2_7 --> B2_8 --> B3 --> B4 --> B5
    end

    subgraph 检索层["🔍 检索层（Zvec 混合检索）"]
        C1["FTS 全文检索<br/>jieba 分词 + BM25 打分"]
        C2["向量语义搜索<br/>multilingual-MiniLM → HNSW"]
        C3["RRF 融合排序<br/>两路排名取长补短"]
        C4["双链邻居扩展<br/>实时从[[双链]]计算"]
        C5["实体搭桥扩展<br/>共享概念实体→隐含关联"]
        C1 --> C3
        C2 --> C3
        C3 --> C4
        C4 --> C5
    end

    subgraph 消费层["💡 消费层（你怎么用知识）"]
        D1["#search 混合搜索"]
        D2["#ask 智能问答<br/>ask.py判断复杂度+覆盖度+缺口"]
        D3["#start 深度带读<br/>+相似文档推荐"]
        D4["#复盘 沉淀回写<br/>深度解读版→ conversations/<br/>（已纳入Zvec索引）<br/>待深挖话题→ wiki/ideas.md<br/>（原料→筛选→验证→归类→进池）"]
        D5["#bridge 发现隐含关联"]
        D6["#lint 差异扫描"]
        D8["#index 索引管理"]
        D9["#stars GitHub Stars同步"]
    end

    subgraph 输出层["🚀 输出层（知识怎么输出）"]
        E2["#evergreen 常青综述"]
    end

    摄入层 -->|"新文章"| 编译层
    编译层 -->|"编译后的总结+概念"| 检索层
    检索层 -->|"搜索/检索结果"| 消费层
    消费层 -->|"带读沉淀/素材"| 输出层
    输出层 -->|"新内容"| 检索层
```

---

## 三、编译流程图（含 RAG 索引）

编译一篇文章时，AI 内部怎么走。

```mermaid
flowchart LR
    subgraph Step1["Step 1: 前置"]
        S0A["convert.py 格式转换+英文翻译<br/>读取 wiki/index.md + wiki/overview.md<br/>+ purpose.md"]
    end

    subgraph Step2["Step 2: 分析"]
        S1A["读取源文件"] --> S1B["对照知识库上下文<br/>已有概念？6类分析关系？矛盾？"]
        S1B --> S1C["输出结构化分析"]
    end

    subgraph Step3["Step 3: 生成"]
        S2A["读取 Step 2 分析"] --> S2B["读取 COMPILE.md 模板<br/>（模板A:短文 / 模板B:视频逐字稿）"]
        S2B --> S2C["生成编译文档<br/>status: compiled"]
        S2C --> S2D["概念页处理（2026-08-02标准）<br/>3条件判定: 抽象性+可复用性+可定义性<br/>排除: 工具/产品/人名/项目名<br/>可零概念不硬凑 / 已有→重写"]
        S2D --> S2E["矛盾标记+尝试解决"]
    end

    subgraph Step3_5["Step 3.5: 实体索引写入"]
        E1["entity_index.py --file<br/>提取概念+event_text+role"] --> E2["写入 entity_index.db<br/>（零额外LLM调用）"]
    end

    subgraph Step4["Step 4: RAG 索引"]
        R1["embed.py 临时加载模型<br/>（峰值~650MB）"] --> R2["fastembed 生成向量<br/>（384维，本地ONNX）"]
        R2 --> R3["upsert 到 Zvec<br/>（FTS+向量同时建好）"]
        R3 --> R4["释放模型内存<br/>（回落~80MB）"]
    end

    subgraph Step5["Step 5: 语义去重检测"]
        D1["search.py --check-dup<br/>语义去重检测"]
    end

    subgraph Step6["Step 6: 增量关联与进化"]
        D2["search.py 混合检索<br/>5类关系+无关系跳过（2026-08-02修订）<br/>补充→自动融入正文+双链<br/>推翻→矛盾标记<br/>对比/时序→双向建链<br/>同主题互证→双向建链<br/>无关系/主题擦边→不建链<br/>质量兜底: #lint每周审查"]
    end

    subgraph Step7["Step 7: 全局更新"]
        S3A["更新 wiki/overview.md"] --> S3B["更新 wiki/index.md"]
        S3B --> S3C["写入 wiki_log.md<br/>更新 processed.md(SHA256)"]
    end

    subgraph Step8["Step 8: 弱关联提醒（增量织网）"]
        S4A["health.py 检测问题"] --> S4B["Zvec 语义匹配找隐含关联"]
        S4B --> S4C["auto-link 提取双链<br/>（零LLM）"]
    end

    subgraph Step9["Step 9: 分类路由校验"]
        S5A["health.py --check category_routing"] --> S5B["YAML category ≠ 目录？"]
        S5B -->|"是"| S5C["自动修正 YAML<br/>（以目录为准）"]
        S5B -->|"否"| S5D["✅ 通过"]
    end

    Step1 --> Step2 --> Step3 --> Step3_5 --> Step4 --> Step5 --> Step6 --> Step7 --> Step8 --> Step9
```

**人话版**：

```
旧流程（一锅炖）：
  AI 读完原文 → 直接生成总结 → 完事
  问题：不知道知识库里已有什么，容易重复或遗漏

新流程（十步走）：
  Step 1：AI 先做前置（convert.py 格式转换+英文翻译+读取知识库上下文：已有概念、overview、purpose）
  Step 2：AI 分析"这篇文章讲了什么，和已有知识什么关系，有没有矛盾"（6类分析关系：引用/补充/推翻/对比/时序/包含层级）
  Step 3：AI 基于分析生成总结，概念页按2026-08-02判定标准处理（3条件：抽象性/可复用性/可定义性；排除工具/产品/人名/项目名；可零概念不硬凑），已有概念重写而非追加，矛盾标记
  Step 3.5：entity_index.py 写入实体索引（概念+一句话概括+core/mention角色，零LLM）
  Step 4：embed.py 自动生成向量入库 Zvec，编译完立刻可搜
  Step 5：检测新文档是否和已有文档语义重复，提醒合并
  Step 6：增量关联与进化——5类关系全自动处理+无关系跳过（2026-08-02修订）：
    补充→已有文档正文自动融入新信息（重写对应段落）+双向建双链
    推翻→矛盾标记，提示 #lint
    对比→双向建双链+注明"不同角度"
    时序→双向建双链+注明"后续发展"
    同主题互证→双向自动建双链+注明"同主题互证"
    无关系/主题擦边→不建链，静默跳过
    质量兜底：#lint 每周自动审查最近7天自动进化内容，发现问题自动修正
  Step 7：自动更新 overview/index/log
  Step 8：弱关联提醒（增量织网）——发现隐含关联、Zvec 语义匹配建议建链、提取双链
  Step 9：分类路由校验——YAML category 与目录不一致则自动修正

  好处：AI 知道知识库已有"注意力经济"，不会重复创建"注意力"概念
  好处：编译完立刻能搜到，不用等
```

---

## 四、搜索与问答流程图

从输入关键词到获得答案的完整路径。

```mermaid
flowchart TD
    Q["用户输入"] --> Type{判断类型}

    Type -->|"#search 关键词"| Search
    Type -->|"#ask 问题"| Ask
    Type -->|"#start 文件名"| Start

    subgraph Search["搜索流程（Zvec 混合检索）"]
        S1["FTS 全文检索<br/>jieba 分词 + BM25 打分<br/>→ 字面匹配候选"] --> S3["RRF 融合排序<br/>两路排名合并"]
        S2["向量语义搜索<br/>query→384维向量→HNSW<br/>→ 语义匹配候选"] --> S3
        S3 --> S4["双链邻居扩展<br/>从[[双链]]实时计算"]
        S4 --> S4B["实体搭桥扩展<br/>共享概念实体→隐含关联"]
    end

    subgraph Ask["问答流程（ask.py 智能路由）"]
        A1["ask.py 问诊"] --> A2a{"complexity?"}
        A2a -->|"simple"| A3a["直接回答<br/>+引用来源 📎"]
        A2a -->|"complex"| A3b["引导确认<br/>列出来/文档/步骤"]
        A2a -->|"comparative"| A3c["对比分析<br/>相同点+不同点+适用场景"]
        A3b --> A4["深度整合回答<br/>+引用来源 📎"]
        A1 --> A5{"coverage?"}
        A5 -->|"insufficient"| A6["主动建议搜索<br/>⚠️ 要不要我去网上搜？"]
        A5 -->|"partial/complete"| A7["继续回答"]
    end

    subgraph Start["带读流程（+推荐）"]
        ST0["已沉淀过滤<br/>check_distilled.py --check<br/>distilled → 拦截不重复带读"] --> ST1["读取文档内容"]
        ST1 --> ST2["search.py --similar<br/>向量搜索相似文档"]
        ST2 --> ST3["展示相似文档推荐<br/>📎 相关推荐"]
        ST3 --> ST4["四步带读法<br/>用户主导答疑：拆解→确认理解<br/>→问有无疑问→用户提问则解答<br/>延伸跑题→标记价值→拉回主线"]
    end

    S4B --> R1["展示搜索结果<br/>标题+相关性理由+摘要"]
    A3a --> R2a["展示回答+来源"]
    A4 --> R2b["展示深度整合回答+来源"]
    A3c --> R2c["展示对比分析+来源"]
    A6 --> R2d["建议 #research 补充"]
    ST4 --> R3["逐段精讲+用户主导答疑"]

    S1 -.->|"降级: Zvec 不可用"| S5["CJK Bigram 搜索<br/>2字滑动窗口+英文分词"]
    S5 -.->|"再降级"| S6["Hermes 直接读文件判断"]
```

**人话版**：

```
#search（搜文档）：
  你输入关键词 → Zvec 混合检索（jieba FTS + 向量 + RRF）→ 双链邻居扩展 → 实体搭桥扩展 → Top-5
  例子：搜"信息过载"→ FTS 找字面匹配 + 向量找语义匹配 → RRF 融合 → 双链扩展关联文档 → 实体搭桥发现共享概念的文档 → 能找到"认知Commons"和"慢速内容"

#ask（问问题）：
  你提问 → ask.py 问诊（检索+复杂度判断+覆盖度评分+缺口检测）
    simple（"X是什么"）→ 直接回答+引用来源
    complex（"怎么做XX"）→ 先引导确认（列出来/文档/步骤）→ 深度整合回答
    comparative（"X和Y什么关系"）→ 对比分析（相同点+不同点+适用场景）
    coverage不足 → 主动建议"要不要我去网上搜一下？"

#start（带读）：
  你指定文件 → check_distilled.py 已沉淀过滤（distilled 拦截）→ 确认开始带读时 --mark 打标记 → 读内容 → 向量搜索相似文档 → 实体搭桥扩展 → 四步带读法（用户主导答疑，延伸分流，复盘统一处理）

搜索架构（已上线）：
  四级流水线：Zvec 混合检索（FTS+向量+RRF）→ 双链邻居扩展 → 实体搭桥扩展 → Top-K
  实体搭桥：从搜索结果提取概念实体，SQL join 找共享实体的其他文档，三层过滤（≥2共享实体/1共享+双方core/高频降权）
  降级链：Zvec+双链+实体搭桥（完整）→ Zvec+双链（实体索引缺失，自动跳过搭桥）→ Zvec（无双链，仅混合检索）→ bigram（+双链扩展）→ Hermes 直接读文件判断（Agent 层兜底），多级降级不会报错

6个 RAG 能力：
  ① 语义搜索 — #search 自动生效
  ② 相似文档推荐 — #start 带读时自动触发（含 GitHub Stars 项目推荐，带⭐标识）
  ③ 智能问答 — #ask ask.py判断复杂度（simple/complex/comparative），覆盖不足主动建议搜索
  ④ 语义去重 — #ingest 入库时自动检测
  ⑤ 增量关联与进化 — #ingest 入库时自动检测5类关系（补充/推翻/对比/时序/同主题互证）+无关系跳过（2026-08-02修订），全自动执行，#lint每周审查兜底
  ⑥ 实体搭桥扩展 — #search/#ask/#start 自动生效，通过共享概念实体发现双链没覆盖的隐含关联
```

---

## 五、RAG 技术架构图

搜索底层的技术实现。

```mermaid
graph TB
    subgraph 索引写入["索引写入（embed.py）"]
        W1["wiki/ 页面内容"] --> W2["fastembed<br/>multilingual-MiniLM<br/>本地ONNX推理"]
        W2 --> W3["384维向量"]
        W3 --> W4["Zvec upsert<br/>FTS(jieba)+向量同时建好"]
    end

    subgraph 查询["查询（search.py）"]
        Q1["用户查询"] --> Q2["fastembed<br/>生成 query 向量"]
        Q2 --> Q3["Zvec MultiQuery"]
        Q3 --> Q4["FTS 路径<br/>jieba+BM25"]
        Q3 --> Q5["向量路径<br/>HNSW 近邻"]
        Q4 --> Q6["RRF 融合"]
        Q5 --> Q6
        Q6 --> Q7["双链邻居扩展"]
    end

    subgraph 数据["数据层"]
        ZV["Zvec 数据库<br/>.llm-wiki/zvec_db/"]
        EC["索引缓存<br/>.llm-wiki/embed-cache.json"]
        MC["模型缓存<br/>&lt;data_root&gt;/.llm-wiki/fastembed_cache/<br/>约 220MB 一次性"]
    end

    W4 --> ZV
    Q3 --> ZV
    ZV --> EC
    W2 -.-> MC
    Q2 -.-> MC
```

**关键参数**：

| 参数 | 值 | 说明 |
|------|---|------|
| Embedding 模型 | multilingual-MiniLM-L12-v2 | 384维，中英混合好，区分度0.48 vs 0.09 |
| 向量索引 | HNSW + COSINE | Zvec 内置，无需配置 |
| FTS 分词 | jieba | Zvec 内置，比 bigram 精准 |
| 融合算法 | RRF (rank_constant=60) | Zvec 内置 RrfReRanker |
| Doc ID | doc_+MD5(page_id)[:8] | Zvec 不支持中文 ID，用哈希编码 |
| 模型内存 | 峰值~650MB，释放后~80MB | 临时加载，用完 gc.collect() |
| 搜索速度 | ~3s（含 query embedding） | 主要耗时在模型加载 |

---

## 六、文件组织与平台适配

指令系统和多 Agent 平台适配的架构。

```mermaid
graph TB
    subgraph 指令系统["指令系统（commands/ 目录）"]
        CMD["system/commands/<br/>12个指令独立文件"]
        SKILL["system/SKILL.md<br/>指令索引"]
        CMD --> SKILL
    end

    subgraph 平台入口["平台入口文件（薄路由层）"]
        CLAUDE["CLAUDE.md<br/>Claude Code 及兼容约定"]
        AGENTS["AGENTS.md<br/>Codex / OpenCode / 其它"]
    end

    subgraph Adapter["Adapter 构建系统"]
        BUILD["scripts/build_adapters.py<br/>1份源 → 入口文件 + 各 agent 原生斜杠命令"]
    end

    SKILL --> CLAUDE
    SKILL --> AGENTS
    BUILD --> CLAUDE
    BUILD --> AGENTS
```

**人话版**：

```
改指令：只改 system/commands/xxx.md 一个文件
新增指令：加一个 .md + 更新 SKILL.md 索引
换 Agent：加一个薄路由文件，或运行 build_adapters.py 自动生成
开源时：build_adapters.py 加新平台 adapter 配置即可
```

---

## 七、基础设施层

支撑整个系统运行的底层脚本。

```mermaid
graph LR
    subgraph 脚本层["Python 脚本层"]
        U["_utils.py<br/>共享工具函数<br/>SHA256/CJK bigram/双链提取/路径常量"]
        S["search.py<br/>Zvec 混合检索 + bigram 降级<br/>相似文档推荐 / 去重检测 / 实体搭桥扩展"]
        E["embed.py<br/>批量向量化 + Zvec 入库<br/>全量/增量/单文件/重建"]
        EI["entity_index.py<br/>实体索引读写 + 搭桥搜索<br/>SQLite三表 / 三层过滤 / role推断"]
        ZC["zvec_config.py<br/>Zvec schema/配置/doc ID编码"]
        H["health.py<br/>纯确定性健康检查<br/>0 LLM, <0.2秒<br/>含分类路由校验+自动修正+实体索引一致性"]
        W["watch.py<br/>INBOX监听<br/>watchdog"]
        C["convert.py<br/>PDF/DOCX/PPTX → Markdown"]
        B["build_adapters.py<br/>平台适配器构建"]
        D["dashboard.py<br/>看板HTML生成"]
        GS["github_stars_sync.py<br/>GitHub Stars入库<br/>去重+分类+中文标签"]
    end

    subgraph 数据层["数据文件层"]
        PM["processed.md<br/>SHA256格式<br/>文件处理记录"]
        WL["wiki_log.md<br/>操作日志"]
        ZV["Zvec 数据库<br/>.llm-wiki/zvec_db/<br/>FTS+向量索引"]
        EIDB["entity_index.db<br/>.llm-wiki/entity_index.db<br/>实体索引（SQLite三表）"]
        EC["索引缓存<br/>.llm-wiki/embed-cache.json<br/>page_id→sha256"]
        GS_DIR["github-stars/<br/>GitHub Stars仓库<br/>12分类+中文标签"]
    end

    U --> S
    U --> E
    U --> H
    U --> W
    U --> EI
    ZC --> S
    ZC --> E
    EI --> S
```

---

## 八、GitHub Stars 同步流程图

`#stars` 触发时，从GitHub API到本地入库的完整路径。

```mermaid
flowchart TD
    START["用户 #stars"] --> API["GitHub API<br/>拉取star列表<br/>（gh auth token）"]
    API --> DIFF["去重对比<br/>API列表 vs 库内已有<br/>（repo字段做差集）"]

    DIFF --> NEW["新增项目"]
    DIFF --> SKIP["已有项目 → 跳过"]
    DIFF --> UNSTAR["取消star → 标记归档"]

    NEW --> README["读README前200行<br/>（GitHub API）"]
    README --> CLASSIFY["自动分类<br/>① topics映射表<br/>② 描述关键词<br/>③ LLM从已有分类选<br/>④ other兜底"]
    CLASSIFY --> LLM["LLM打中文描述+标签<br/>（LLM_API_BASE/KEY/MODEL<br/>→ Hermes config.yaml custom_providers）<br/>描述：2-3句40-80字<br/>标签：3-5个中文场景标签"]
    LLM --> |"API失败降级"| LLM_FALL["跳过标签<br/>仅英文描述入库"]
    LLM --> GEN["生成Markdown<br/>github-stars/[分类]/[owner]-[repo].md"]
    LLM_FALL --> GEN
    GEN --> INDEX["embed.py --incremental<br/>自动向量索引<br/>（项目 venv 内）"]

    style NEW fill:#4CAF50,color:#fff
    style SKIP fill:#9E9E9E,color:#fff
    style UNSTAR fill:#FF9800,color:#fff
    style LLM_FALL fill:#FFC107,color:#333
```

**人话版**：

```
#stars 触发 → 拉GitHub API → 对比库里已有的 → 三种情况：
  新增的 → 读README → 自动分类 → LLM打中文描述+标签 → 生成md → 更新索引
  已有的 → 跳过（零token）
  取消star的 → 标记归档

增量更新：只有第一次全量，后续只处理新增的几个项目

⚠️ 动作分类移动/整改（把已有项目从 A 分类挪到 B 分类）后：
   必须 python scripts/entity_index.py --rebuild 重建实体索引
   （stars 常规同步只写 Zvec 向量、不写 entity_index；不重建会留 死行 + 新路径失联）
   验证：python scripts/health.py --check entity_index → 应输出 Entity index OK
```

---

## 九、检索策略区分图

用户提问时，Agent如何判断搜wiki还是搜github-stars。

```mermaid
flowchart TD
    Q["用户提问"] --> CHECK{"问题是否包含<br/>项目/star/收藏/GitHub？"}

    CHECK -->|"是"| GS["优先搜 github-stars/<br/>Zvec混合检索"]
    CHECK -->|"否"| WIKI["优先搜 wiki/<br/>Zvec混合检索"]
    CHECK -->|"不确定"| BOTH["两个都搜<br/>合并结果"]

    GS --> MERGE["Zvec 混合检索<br/>FTS(jieba+BM25) + 向量(HNSW) + RRF融合"]
    WIKI --> MERGE
    BOTH --> MERGE

    MERGE --> RESULT["展示结果<br/>路径前缀区分来源<br/>github-stars/xxx 或 wiki/xxx"]

    style CHECK fill:#2196F3,color:#fff
    style GS fill:#4CAF50,color:#fff
    style WIKI fill:#9C27B0,color:#fff
    style BOTH fill:#FF9800,color:#fff
```

**检索命中维度**（描述和标签互补）：

| 搜索方式 | 命中维度 | 举例 |
|---------|---------|------|
| FTS关键词 | 标签 + 描述 + topics | 搜"小红书运营" → 命中标签 |
| 向量语义 | 描述 + 标签 + 全文 | 搜"视频制作" → 语义匹配"视频生成" |
| 描述兜底 | 中文描述 | 搜"批量渲染" → 命中描述里的场景词 |
| 标签兜底 | 中文标签 | 搜"求职" → 命中标签"面试准备" |

**GitHub Stars文档结构**（每个md文件）：

```
---
title: 项目名
type: github-star
repo: owner/name          ← 去重key
category: ai-agents       ← 分类目录
tags: [标签1, 标签2, ...]  ← 中文场景标签（LLM生成）
---
# 项目名

英文描述（原文）
中文描述（LLM生成，2-3句，覆盖核心功能+适用场景+用户群体）

🔗 [GitHub](https://github.com/xxx/yyy)
```

---



```mermaid
flowchart TD

    RUBRIC --> SCORE["1. 打分<br/>7维 rubric 对草稿打分"]

    SCORE --> PREDICT["2. 盲预测（immutable）<br/>7组件: composite/CTR/互动/涨粉/场景/锚点/信心<br/>⚠️ 预测必须在看到实际数据之前写完<br/>写完后不可修改，只能往复盘段追加"]

    PREDICT --> PUBLISH["3. 发布登记<br/>拍摄buffer管理（拍了 vs 发了分离）<br/>发布元数据登记"]

    PUBLISH --> RETRO["4. 复盘（T+3d）<br/>用户填入实际数据<br/>预测 vs 实际对比<br/>偏差分析"]

    RETRO --> BUMP{"5. 升级 rubric？<br/>校准池 ≥ 5篇数据？"}
    BUMP -->|"是"| BUMP_DO["全量重打校准池<br/>跨模型独立审核<br/>≥4/5样本排序一致才通过"]
    BUMP -->|"否"| SCORE2["继续打分下一篇"]
    BUMP_DO --> SCORE2

    style INIT fill:#FF9800,color:#fff
    style PREDICT fill:#2196F3,color:#fff
    style RETRO fill:#9C27B0,color:#fff
    style BUMP fill:#4CAF50,color:#fff
```


```
       ↓
       ↓
       ↓
发布后 "复盘" → T+3d数据回收 → 预测vs实际对比
       ↓
积累5+篇 → "升级rubric" → 校准池全量重打 + 跨模型审核
```


| 原因 | 说明 |
|------|------|
| 完整5阶段闭环 | 打分→预测→发布→复盘→进化rubric，阉割成YAML字段会丢失核心能力 |
| immutable日志 | 预测是独立文件不可修改，写进脚本YAML会破坏不可变性 |
| rubric进化 | 校准池全量重打+跨模型审核，需要独立状态管理 |

**三条不可妥协原则**：
1. **盲预测**：预测必须在看到实际数据之前写完，写完不可修改
2. **升级=全量重打**：rubric升级时校准池所有有实绩数据的样本必须用新公式重打
3. **rubric是工作台不是博物馆**：被新数据推翻的观察直接删掉，不留考古层
