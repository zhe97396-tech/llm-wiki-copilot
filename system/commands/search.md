---
name: search
description: "全库混合检索：Zvec（jieba FTS + 向量语义 + RRF融合），降级回 CJK bigram"
triggers_en: ["search", "搜索", "find in wiki", "搜索知识库"]
category: retrieval
---

# 🔍 全库混合检索：`#search` 或 `#搜索`

**触发时机**：用户发送 `#search [关键词]` 或 `#搜索 [关键词]`。

**检索范围**：`wiki/` + `github-stars/`（两个目录均纳入Zvec索引）

**检索策略**：
- 问题明确提到"项目/工具/star/收藏" → 优先搜 `github-stars/`
- 通用知识问题 → 优先搜 `wiki/`
- 不确定时 → 两个都搜，合并结果

**执行动作**：
1. **调用搜索脚本**：执行 `PYTHONPATH="" .venv/Scripts/python.exe scripts/search.py "用户关键词" --top 5`，内部使用 Zvec MultiQuery（jieba FTS + 向量语义 + RRF融合）进行混合检索。
2. **AI 可选精排**：Zvec RRF 已融合排序，AI 精排锦上添花。如果用户查询的词和文档内容语义相关但字面不同（如"信息成瘾"≈"注意力经济"），向量搜索已能识别这种语义关联。
3. **展示结果**：按相关性从高到低展示 Top-5，每条包含：
   - 文档标题
   - 相关性理由（一句话说明为什么相关）
   - 一句话摘要
4. **降级**：如果 Zvec 不可用，自动降级到 CJK bigram 搜索。如果搜索脚本执行失败，Hermes 直接用 read_file 读取所有文档标题+摘要，AI 判断相关性。

**前台回复规范**：
"🔍 搜索完成！找到 X 篇相关文档：
1. 文档标题 — 相关性理由
2. 文档标题 — 相关性理由
..."
