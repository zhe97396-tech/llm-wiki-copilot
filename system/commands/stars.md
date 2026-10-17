---
name: stars
description: "GitHub Stars 同步入库：API拉取→去重→分类→打标签→入库→索引"
triggers_en: ["stars", "星标", "收藏", "github stars"]
category: core
---

# ⭐ GitHub Stars 同步入库：`#stars` 或 `#星标`

**触发时机**：用户发送 `#stars`、`#星标`、`#收藏`，或要求同步GitHub收藏项目。

**执行动作**：运行 `PYTHONPATH="" .venv/Scripts/python.exe scripts/github_stars_sync.py`，自动完成以下流程。

⚠️ **必须用 `.venv/Scripts/python.exe`**：`python` = Hermes venv，**没有 PyYAML** → `llm_api` 读不到 Hermes config 里的 LLM 配置 → Step 3 的中文标签、LLM 兜底分类**全部静默降级**（只打印一行警告，文件照样入库但 `tags: []`）。`.venv/Scripts/python.exe` 同样可用。跑完可用 `PYTHONPATH="" .venv/Scripts/python.exe scripts/llm_api.py` 自检（应打印 base_url / api_key: SET / model；出现「缺少 PyYAML」告警 = 解释器用错）。历史教训：2026-08-01/08-30/09-17/10-09 四次同步都因裸 `python` 导致标签全空。

1. **Step 1：拉取GitHub API**
   - 获取当前用户（`config.yaml` 里 `integrations.github_stars.username` 配置的用户名）的所有starred项目
   - 需要 GitHub Token（`gh auth login` 或 `GITHUB_TOKEN` 环境变量）

2. **Step 2：去重对比（repo 键统一小写）**
   - 扫描 `github-stars/` 目录下已有项目的 `repo` 字段
   - 与API返回列表做差集：新增/已有/取消star
   - ⚠️ **repo 键统一小写比较**：GitHub repo 名**大小写不敏感**，`existing` 键与 `api_repos` 均 `.lower()` 归一（2026-10-09 修复：曾因 `Hmbown/CodeWhale` vs `Hmbown/Codewhale` 大小写差异，同一项目重复入库两份文件）
   - ⚠️ **已知局限**：仓库**改名/换 owner** 后 `full_name` 变化（如 `Hmbown/CodeWhale` → `codewhale-hq/Codewhale`）仍会被判为"新增"→ 再生成一份文件；脚本未做改名追踪（如需可加 repo_id 稳定键）

3. **Step 3：新增项目处理**
   - 请求README前200行（容错：无README时降级）
   - 自动分类（topics映射→描述推断→LLM兜底；**LLM兜底读README摘要判断**，见分类映射表）
   - LLM打3-5个中文场景标签（自动从Hermes config.yaml读取当前provider，环境变量LLM_API_BASE/KEY/MODEL优先，失败时降级跳过标签）

4. **Step 4：生成Markdown入库**
   - 按模板生成到 `github-stars/[分类]/[owner-name].md`
   - 生成 `_index.md` 总览索引

5. **Step 5：取消star处理（幂等，2026-10-09 修复）**
   - 已取消star的项目标记 `type: github-star-archived`
   - **幂等要求**：已是 `github-star-archived` 的文件**跳过不写盘**；历史叠加态（`github-star-archived-archived…`，旧 `replace()` 子串逻辑留下的）自动**归一**为规范值，汇报里单列「归一：N」
   - ⚠️ **type 值必须保持规范**：`search.py` 的 `SOURCE_TIER` 查表和 `--exclude-type` 都是**精确匹配**——值被污染（后缀叠加）会落表默认 tier **1.0**（比正常归档 0.5 还高）=归档项目被错误提权。归档改用正则精确匹配整个 type 值，禁止回退成 `content.replace("type: github-star", …)` 子串替换

6. **Step 6：Zvec索引更新（自动）**
   - 脚本自动调用 `embed.py --incremental` 更新向量索引，同步完立即可搜
   - 如果自动索引失败，可手动运行 `#index` 补索引

7. **Step 7：分类移动后的实体索引重建（防死行）**
   - `entity_index.db` 的 github-stars 部分**只在 `entity_index.py --rebuild` 时生成**，`github_stars_sync.py` 常规同步不写实体索引
   - 对已有项目做**分类移动/整改**（如 2026-08-29 ai-agents 84→49 的搬迁）时：旧路径的 chunk 行不会自动清理、新路径也不会自动写入 → `health.py` 会报「N stale chunks pointing to deleted files」，且被移动的项目在实体搭桥里失联
   - 整改收尾**必须**执行 `python scripts/entity_index.py --rebuild`，再跑 `python scripts/health.py --check entity_index` 验证（应输出 `Entity index OK`、无 stale）

**脚本选项：**
- `--no-llm-tags`：跳过LLM打中文标签（省token）
- `--full`：忽略断点续传，全量重新处理
- `--output-dir DIR`：指定入库目录

**分类映射表：**

| 分类目录 | 匹配 topics / 关键词 |
|---------|---------------------|
| ai-agents | ai-agents, ai-agent, agent, agentic-ai |
| claude-code-skills | claude-skills, agent-skills, claude-skill, claude-code-skill, skills, skill, codex（⚠️ 裸 `claude-code` topic 不算：只表示"兼容Claude Code"的客户端/工具，不是技能包） |
| mcp-tools | mcp |
| rag | rag |
| llm-tools | llm, prompt-engineering, openai（⚠️ `claude`/`anthropic` 同 `claude-code`：兼容性标签不算形态，2026-08-29 移除） |
| browser-automation | browser-automation, web, scraping |
| devops | devops, ci-cd, docker, kubernetes |
| tools-utilities | cli, developer-tools, tools, utility |
| xiaohongshu | xiaohongshu |
| resume-career | resume, career, job |
| image-gen | image-generation, ai-art, stable-diffusion |
| other | 无法归类的 |

**检索策略：**
- 问题明确提到"项目/工具/star/收藏" → 优先搜 `github-stars/`
- 通用知识问题 → 优先搜主 Wiki
- 不确定时 → 两个都搜，合并结果

**输出汇报：**
> ⭐ GitHub Stars 同步完成！新增 [X]，跳过 [Y]，归档 [Z]。
