---
name: ingest
description: "从 00-INBOX 批量编译入库：格式转换→分析→编译→全局更新"
triggers_en: ["ingest", "入库", "编译", "compile"]
category: core
---

# 📥 批量编译入库：`#ingest` 或 `#入库`

**触发时机**：用户发送 `#ingest` 或 `#入库`，或手动将文件放入 `00-INBOX/` 后要求编译。

**执行动作**：严格按照 `system/PROMPT.md` 的 Step 1-9 执行完整编译流程。

1. **Step 1：前置动作**
   - 获取当前系统日期
   - 执行 `.venv/Scripts/python.exe scripts/convert.py --inbox`，将 INBOX 中的非 Markdown 文件（PDF/DOCX/PPTX/图片）转为 Markdown（⚠️ 必须用 .venv 的解释器：`python`=Hermes venv 没装 pytesseract/markitdown，图片 OCR 会静默失效返回"未识别到文字内容"）
   - **入口链接查重（2026-08-17 新增，飞书收集管道统一判据）**：对 `00-INBOX/` 中 `feishu_` 来源文件（飞书内容收集管道产物，frontmatter 含 `source:` 链接）执行链接级查重：
     - 抖音链接（v.douyin.com / douyin.com/video/）：解析 video_id → 查 `.llm-wiki/douyin_ledger.db` 账本 + `raw/douyin/` 下 .md 的 frontmatter `video_id`（短链与长链按 video_id 判重，字符串匹配不上）
     - 其他平台：规范化链接（去尾部斜杠/#fragment/utm_*）→ 查 `raw/` 所有 .md 的 frontmatter `source:` 精确匹配 + 00-INBOX 其他文件 source
     - **命中 = 该链接已入库/排队中 → 跳过该文件**，向用户汇报「重复跳过：文件 + 已存在于何处」；未命中才进入编译
     - ⚠️ 去重匹配层是 `raw/`（转录稿/抓取内容 frontmatter source=真实链接），不是 `wiki/`（编译产物 source=平台名）
   - 预扫描 `wiki/ideas.md`（待深挖灵感池）
   - **飞书表格快照落盘位置（2026-08-22 约定）**：处理飞书内容收集池需用 lark-cli 拉表时，命令必须带 `--output .llm-wiki/tmp/<表ID>_<时间戳>.ndjson --overwrite`，快照（.ndjson+.manifest.json）统一落在 `.llm-wiki/tmp/`，禁止在仓库根目录产生散落文件
   - 读取知识库上下文（`system/COMPILE.md`、`wiki/index.md`、`wiki/overview.md`、`purpose.md`）

2. **Step 2：分析与关系判定**
   - 扫描 `00-INBOX/` 中未处理文件（对比 `✅ processed.md`）
   - **图文同篇归类**：`{标题}.md` + 同名前缀 `{标题}_img_N*.md`（图片 OCR 产物）按一篇处理，OCR 文本合并进编译产物，中间件不单独成篇（详见 PROMPT.md Step 2）
   - 对每个文件做结构化分析：核心概念、与已有内容关系、分类路由
   - 如果 INBOX 为空或全部已处理，报告"无新文件待入库"并停止
   - **注意：** `github-stars/` 目录不参与 #ingest 编译流程，该目录有独立的 `#stars` 指令

3. **Step 3：双轨编译与生成**
   - 重命名并归档原文件到 `raw/`（非 Markdown 原件归档至 `raw/assets/`）
   - 选择模板：视频/逐字稿→模板B，其他→模板A
   - 生成编译文档至 `wiki/summaries/[category]/`
   - 处理概念页：新概念新建，已有概念重写合并
   - 处理矛盾标记

3.5. **Step 3.5：实体索引写入**
   - 对每个新生成/更新的 wiki 页面，执行 `.venv/Scripts/python.exe scripts/entity_index.py --file <wiki页面路径>`（内部会提取 event_text + 概念列表 + 推断 role，写入 entity_index.db）
   - 概念页也会被索引：event_text 取第一段定义，实体取底部来源双链指向的其他概念
   - 此步骤零额外 LLM 调用，复用编译时已有的概念提取输出
   - 如果 entity_index.py 执行失败，不影响编译结果，仅影响搭桥搜索

4. **Step 4：向量化与 Zvec 入库（RAG 索引）**
   - 对每个新生成/更新的 wiki 页面，执行 `PYTHONPATH="" .venv/Scripts/python.exe scripts/embed.py --file <wiki页面路径>`
   - embed.py 内部会临时加载 fastembed 模型（峰值~650MB内存，用完释放），生成 embedding 向量，并 upsert 到 Zvec（FTS+向量索引同时建好）
   - 如果 embed.py 执行失败，不影响编译结果，仅影响搜索索引（下次 `PYTHONPATH="" .venv/Scripts/python.exe scripts/embed.py` 全量索引可修复）
   - 验证：执行 `PYTHONPATH="" .venv/Scripts/python.exe scripts/search.py "页面标题关键词" --top 3`，确认新页面可被检索到

5. **Step 5：语义去重检测**
   - 对每个新入库的页面，执行 `PYTHONPATH="" .venv/Scripts/python.exe scripts/search.py "<页面路径>" --check-dup`
   - 如果发现语义高度重复的已有文档，向用户报告：
     > ⚠️ **语义重复检测**：新文档与已有文档内容高度相似，建议合并或确认是否为不同角度。

6. **Step 6：增量关联与进化**
   - 对每个新入库的页面，执行 `PYTHONPATH="" .venv/Scripts/python.exe scripts/search.py "新文档标题关键词" --top 5 --backend zvec --exclude-type github-star`
   - **注意：** `--exclude-type github-star`——github-stars 是独立分支（`#stars` 管理），不与主知识库建链，Step 6 搜索必须排除，避免噪音混入关联判断
   - **⚠️ 双链建链铁律（2026-08-02 修订）**：相似度分数只是召回工具，不是建链判据。可建链的关系只有 5 种：**补充 / 推翻矛盾 / 对比 / 时序演化 / 同主题互证**（两篇内容主体是同一主题可互相印证）。5 种都不是 → **不建链，静默跳过**。>0.8 高相似归入 Step 5 语义去重。每条链必须带关系类型注释（"补充了XXX"/"同主题互证"等），供 #lint 验证
   - **补充**：已有文档正文自动融入新信息（重写对应段落）+ 新文档加已有文档双链 + 已有文档对话沉淀区追加关联记录 + 更新向量索引
   - **推翻**：矛盾标记 + 提示 `#lint`
   - **对比**：双向建双链 + 注明"不同角度"
   - **时序/演化**：双向建双链 + 注明"后续发展"
   - **同主题互证**：双向自动建双链 + 注明"同主题互证"
   - **无关系（含主题擦边）**：静默跳过，不建链
   - 所有操作全自动执行，无需用户确认。质量兜底由 `#lint` 定期审查模式保障

7. **Step 7：知识库全局更新**
   - 更新 `wiki/overview.md`
   - 更新 `wiki/index.md`
   - 写入 `✅ processed.md` 和 `wiki_log.md`
   - 清理翻译中间文件：`.venv/Scripts/python.exe scripts/convert.py --cleanup-translations`

8. **Step 8：弱关联提醒**（增量织网）
   - 对新入库的文档，使用 Zvec 搜索语义相关但 Step 6 未覆盖到的弱关联文档对（相似度 0.4-0.6 区间，作为召回候选）
   - 向用户展示关联建议（⚠️ 候选仍需符合双链铁律的 5 种关系之一，主题擦边不推荐），用户确认后才建双链
   - 与 `#bridge` 的区别：Step 8 只看新文档的增量关联（零额外搜索成本），`#bridge` 做全库扫描

9. **Step 9：分类路由校验**
   - 对本次新生成的总结文档执行 `PYTHONPATH="" .venv/Scripts/python.exe scripts/health.py --check category_routing --files <文件路径>`
   - 脚本会自动修复 YAML category 与目录不一致的问题（以目录为准修正 YAML）
   - 如存在无法自动修复的问题，在汇报中追加提示

**输出汇报**：
> 📥 入库与编译完成，本次使用模板A处理 [X] 份文件，模板B处理 [Y] 份文件。新增概念 [Z] 个，更新已有概念 [W] 个，发现矛盾 [V] 处。

**降级**：
- 如果 `convert.py` 执行失败，跳过格式转换步骤，仅处理已有的 .md 文件
- 如果 INBOX 为空，提示用户"00-INBOX/ 无待处理文件，请先将内容放入 INBOX"
