# GitHub Stars 同步集成

> 把你 GitHub 上 star 过的项目同步进知识库，让它们参与检索与带读。
> **脚本已在引擎内**：`scripts/github_stars_sync.py`（不需要额外安装）。

## 配置

```yaml
integrations:
  github_stars:
    enabled: true
    username: "your-github-username"
    token_env: GITHUB_TOKEN     # 可选：提高 API 速率限制
```

或直接给环境变量：`GITHUB_USERNAME=your-name`（优先级高于 config.yaml）。

## 它做什么

```
GitHub API 拉取 starred 列表
  → 与本地已有文件去重（repo 键统一小写）
  → 自动分类（topics 精确匹配 → 描述关键词 → LLM 兜底）
  → 读 README
  → LLM 生成中文标签与中文描述（未配 LLM 时仅英文描述）
  → 生成 Markdown → github-stars/[分类]/
  → 自动增量建向量索引
```

## 用法

```bash
# 增量同步（日常用这个）
.venv/Scripts/python.exe scripts/github_stars_sync.py

# 之后在对话里触发 #stars 也可以（Agent 会调这个脚本）
```

## 目录设计

```
github-stars/
├── _index.md              ← 总索引（分类统计）
├── ai-agents/             ← 分类目录（可自定义）
├── claude-code-skills/
└── ...
```

**与主知识库物理隔离**：星标项目不进 `wiki/`。它们参与检索（带权重降权），
但在「检索主知识库内容」的场景可被 `--exclude-type github-star` 排除。

## 已知坑

| 坑 | 说明与对策 |
|---|---|
| **归档必须幂等** | 取消 star 的项目会被标记 `type: github-star-archived`；脚本用正则精确匹配整个 type 值，**不会叠加**成 `-archived-archived`（叠加会让检索权重错乱） |
| **repo 键大小写** | GitHub repo 名大小写不敏感，去重键统一 `.lower()`，否则同一项目会入库两份 |
| **改名未追踪** | 仓库改名/换 owner 后 `full_name` 变化，会被判为「新增」再生成一份（已知局限） |
| **删文件留 ghost** | 删除星标文件后向量库仍有残留，需要 `embed.py --rebuild` 清理 |
| **翻译≠推理** | 中文标签与分类需要模型理解归纳，机器翻译做不了；未配 LLM 时会退化为仅英文描述 |
