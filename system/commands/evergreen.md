---
name: evergreen
description: "知识升华与常青笔记：交叉多篇distilled文章，提取共性规律，生成体系综述"
triggers_en: ["evergreen", "常青", "synthesize"]
category: graph
---

# 🌲 知识升华与常青笔记：`#evergreen` 或 `#常青`

**触发时机**：
1. 用户选中了多篇处于 `distilled` 状态的文章，发送 `#evergreen [宏观主题]`
2. `#lint` 定期审查发现自动进化内容质量问题时，可触发 `#evergreen` 重写修正

**执行动作**：
1. 交叉读取多篇总结笔记（特别是底部的"对话沉淀"精华）。
2. 提取这些文章的共性规律、底层逻辑和矛盾点。
3. 在 `wiki/evergreen/` 下生成 `[宏观主题]_体系综述.md`，使用双向链接密集引用支撑文章。
