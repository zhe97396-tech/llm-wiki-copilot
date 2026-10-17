---
name: ask
description: "知识库问答：脚本判断复杂度，简单直接答，复杂先引导再整合，缺口主动建议搜索"
triggers_en: ["ask", "问", "query", "提问"]
category: retrieval
---

# ❓ 知识库问答：`#ask` 或 `#问`

**触发时机**：用户发送 `#ask [问题]` 或 `#问 [问题]`。

**执行动作**：

1. **运行问诊脚本**：
   执行 `PYTHONPATH="" .venv/Scripts/python.exe scripts/ask.py "用户问题"`，获取结构化分析结果：
   - `complexity`：simple / complex / comparative
   - `coverage` + `coverage_level`：知识库覆盖度（complete / partial / insufficient）
   - `gap_topics`：缺口主题词
   - `results`：检索结果（已含confidence标签、已按score排序）
   - `stats`：命中数、高信数、目录分布、概念页列表

2. **根据 complexity 执行不同路径**：

   ### simple（简单型）
   - 直接基于检索结果回答
   - 每个论点标注引用：`📎 来源：文档名`
   - 回答末尾标注覆盖度和缺口

   ### complex（复杂型）
   - **先引导确认**，不直接回答。向用户提问：
     - "你想要哪种？1)帮我列出来 2)总结成文档 3)给我实操步骤"
     - 如果问题明确暗示了输出形式（如"怎么做XX"→实操步骤，"整理一下"→列出来），可跳过确认直接按对应形式输出
   - 用户确认后，按选择深度整合：
     - **列出来**：每篇文档讲了什么 + 它们之间的关系（前置/并列/递进）
     - **总结成文档**：整合多文档内容，串联逻辑，输出可保存的文档
     - **实操步骤**：从知识库内容推导出可执行步骤，每步标注来源
   - 所有论点必须标注引用来源

   ### comparative（对比型）
   - 找出概念/文档的相同点和不同点
   - 列出各自的适用场景
   - 标注引用来源

3. **缺口处理**：
   - `coverage_level` = "insufficient" → 主动问用户：
     "这部分知识库内容不够覆盖，要不要我去网上搜一下补充？"
   - 用户同意 → 建议使用 `#research [缺口主题]` 补充
   - 用户不需要 → 标注缺口，继续回答
   - `coverage_level` = "partial" → 在回答末尾标注缺口，不主动建议搜索

4. **未收录处理**：
   如果检索结果为空，明确回答"知识库中未收录此内容"，
   建议用户使用 `#research [主题]` 搜索网络补充。

5. **降级**：
   ask.py 失败 → 回退到 `PYTHONPATH="" .venv/Scripts/python.exe scripts/search.py "问题" --top 10`，
   提示词自行判断复杂度并回答（简单问题直接答，复杂问题先引导确认）。
