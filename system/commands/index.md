---
name: index
description: "Zvec 索引管理：全量/增量/重建/统计，手动控制RAG搜索索引"
triggers_en: ["index", "索引", "reindex", "重建索引"]
category: retrieval
---

# 📇 索引管理：`#index` 或 `#索引`

**触发时机**：用户发送 `#index`、`#索引`、`#index 重建`、`#index 统计` 等。

**执行动作**：

1. **判断子命令**（根据用户输入自动识别）：

| 子命令 | 触发方式 | 执行 | 说明 |
|--------|---------|------|------|
| 增量索引 | `#index` 或 `#索引` | `PYTHONPATH="" .venv/Scripts/python.exe scripts/embed.py --incremental` | 只索引内容变化的页面，速度快 |
| 全量索引 | `#index 全量` | `PYTHONPATH="" .venv/Scripts/python.exe scripts/embed.py` | 扫描所有wiki页面，新文档全入库 |
| 单文件索引 | `#index <文件名>` | `PYTHONPATH="" .venv/Scripts/python.exe scripts/embed.py --file <路径>` | 只索引指定文件 |
| 重建索引 | `#index 重建` | `PYTHONPATH="" .venv/Scripts/python.exe scripts/embed.py --rebuild` | 删除旧索引，从零重建（索引损坏时用） |
| 统计 | `#index 统计` | `PYTHONPATH="" .venv/Scripts/python.exe scripts/embed.py --stats` | 查看Zvec文档数、缓存状态 |

2. **执行脚本**：运行对应命令，等待输出
3. **汇报结果**：向用户展示索引统计（新增X篇、更新X篇、跳过X篇、错误X篇）

**自动触发**：`#ingest` 编译入库完成后，Step 4 会自动对新增/更新的页面执行单文件索引。`#index` 指令用于手动补索引或修复。

**降级**：如果 embed.py 执行失败（如模型加载失败），不影响知识库内容，搜索会自动降级到 CJK bigram。可用 `#index 重建` 修复。

**前台回复规范**：
"📇 索引完成！新增 X 篇，更新 Y 篇，跳过 Z 篇。当前 Zvec 共 N 篇文档。"
