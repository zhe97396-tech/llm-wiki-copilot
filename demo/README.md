# Demo 数据

> 三篇示例内容 + 它们的编译产物，用来**立刻体验**这套系统怎么工作。
> 主题选的是通用的方法论（费曼技巧 / 间隔重复 / 每日复盘），不含任何个人信息。

## 怎么用

### 方式一：直接检索（最快）

```bash
export LLM_WIKI_DATA="<引擎目录>/demo"      # 把数据根临时指向 demo
.venv/Scripts/python.exe scripts/embed.py   # 建索引
.venv/Scripts/python.exe scripts/search.py "怎么才能记得更牢"
```

你应该能看到「间隔重复 / 遗忘曲线」相关页面被命中，
并且结果里包含**通过概念搭桥带出来的关联页面**（比如「主动回忆」）。

### 方式二：并入你自己的知识库

```bash
cp -r demo/wiki/*    <你的知识库>/wiki/
cp -r demo/raw/*     <你的知识库>/raw/
# 然后对 Agent 说：#index 重建
```

### 方式三：看 Agent 怎么读它们

打开 `wiki/summaries/方法论/` 下的任一文件，对照 `system/COMPILE.md`
里的三轨模板 —— 你会发现三种形态走了三个不同模板。

## 这份 demo 演示了什么

| 文件 | 演示点 |
|---|---|
| `summaries/方法论/费曼学习法…` | **模板 A**（短知识文）：1.5 全文脉络 + 核心内容 + 概念关联 |
| `summaries/方法论/间隔重复…` | **模板 B**（深度长文）：按原文逻辑分章节精要梳理 |
| `summaries/方法论/每日复盘清单…` | **模板 C**（工具/清单形态）：用途 → 结构 → 用法 → 关联 |
| `concepts/费曼技巧.md` | 概念页三条件（抽象性 / 可复用性 / 可定义性）都满足 |
| `concepts/遗忘曲线.md` | 概念页，且**被多篇引用**（entity 权重会更高） |
| `concepts/主动回忆.md` | 三篇之间的**桥接概念**，演示实体搭桥如何补向量检索的漏 |
| `wiki/index.md` `wiki/overview.md` | 编译时自动维护的全库索引与概述 |

## 目录结构

```
demo/
├── README.md
├── raw/                          ← 源文件（原文归档）
│   ├── 2026-01-05_web_费曼学习法-教是最好的学.md
│   ├── 2026-01-07_web_间隔重复-遗忘曲线与记忆调度.md
│   └── 2026-01-09_web_每日复盘清单-五分钟三问.md
└── wiki/
    ├── summaries/方法论/        ← 编译产物：总结页
    ├── concepts/                  ← 编译产物：概念页
    ├── index.md
    └── overview.md
```
