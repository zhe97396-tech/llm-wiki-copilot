# MCP 集成：把知识库开放给任意 Agent

> 让 Claude Code / Cursor / Codex / Cline / Hermes 通过 **MCP** 直接检索这个知识库。
> **只读**、stdio 传输、无端口、无守护进程、无鉴权 —— 只在本机可用。

## 它做什么

一个 stdio MCP 服务器（`wikimcp.py`），暴露 5 个只读工具：

| 工具 | 作用 |
|---|---|
| `search` | 混合检索（Zvec：jieba 全文 + 向量 + RRF，证据分层排序），返回 id / 标题 / 分类 / 置信度 / 摘要 |
| `fetch` | 按 id 取全文（正文 + 白名单 frontmatter）；`include_raw=true` 可取 `raw/` 原文 |
| `list_concepts` | 全部概念页 + 一句话定义（知识库的全局地图） |
| `stats` | 页面数、索引文档数、各目录分布 |
| `ask` | 检索 + 复杂度 / 覆盖度 / 缺口判断（回答"这个库够不够回答"） |

它**不自带**检索实现：和主链路共用同一份 `scripts/search.py`、`scripts/ask.py` 与同一个索引，
所以 MCP 搜出来的东西和你自己 `python scripts/search.py` 搜出来的一致。

## 前置：先建索引

```bash
# 用装了 zvec + fastembed 的那个解释器（和 embed.py 同一个）
python scripts/embed.py
```

没建索引时 `search` 会返回 0 条（`stats` 里 `index_docs=0` 就是这个信号）——
`list_concepts` / `fetch` 仍然可用。

## 客户端配置

把 `<EMBED_PYTHON>` 换成上面那个解释器的绝对路径，`<REPO>` 换成仓库根目录：

```json
{
  "mcpServers": {
    "llm-wiki": {
      "command": "<EMBED_PYTHON>",
      "args": ["-E", "-s", "<REPO>/integrations/mcp/wikimcp.py"],
      "env": { "PYTHONPATH": "" }
    }
  }
}
```

- `PYTHONPATH: ""` **不要省**：某些宿主（如 Hermes）会把自己的 venv 注入 `PYTHONPATH`，
  优先级高于你的 embed venv，导致 `fastembed`/`zvec` 加载错版本、语义检索静默降级。
- 数据目录默认取仓库下的 `vault/`；放别处就加 `"LLM_WIKI_DATA": "/path/to/vault"`。

各客户端配置文件位置：

| 客户端 | 位置 |
|---|---|
| Claude Code | `~/.claude.json` 的 `mcpServers` |
| Cursor | `~/.cursor/mcp.json` |
| Cline | VS Code 设置里的 MCP Servers |
| Codex | `~/.codex/config.toml` 的 `[mcp_servers.llm-wiki]` |
| Hermes | `config.yaml` 的 `mcp_servers`（可加 `lazy: true` 用到才启动） |

## 自测（不需要自己的知识库）

用仓库自带的 `demo/` 跑一遍完整链路：

```bash
# 1) 给 demo 建索引
LLM_WIKI_DATA=demo python scripts/embed.py

# 2) 跨进程调用全部工具
LLM_WIKI_DATA=demo LLM_WIKI_PYTHON=<EMBED_PYTHON> python integrations/mcp/smoke_client.py
# → RESULT: PASS
```

`smoke_client.py` 用官方 MCP SDK 启动服务器、握手、列工具、逐个调用，
并用 `fetch` 一个不存在的 id 验证错误路径（必须报错而不是返回空）。

## 范围控制

```
scope=personal  默认，全库
scope=team      只给 wiki/concepts + wiki/summaries + wiki/evergreen
                （排除 conversations / topics / github-stars / system）
```

对外分享知识库、或给团队 bot 用时传 `scope: "team"`。

## 已知边界

- `raw/` 不进索引：搜不到原文，只能 `fetch(id="raw/子目录/文件名", include_raw=true)` 按路径取。
- 进程生命周期由客户端管：客户端启动即拉起（Hermes 可配 `lazy: true` 改成首次调用才起），
  客户端退出随之退出。服务端**没有**自启动 / 监听端口 / 定时任务。
- 模型首次加载约 1–3 秒（之后常驻在子进程里），闲置时内存会被系统换出。
