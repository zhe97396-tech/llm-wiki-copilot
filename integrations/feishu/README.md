# 飞书集成：内容收集管道

> **可选集成**。默认关闭。依赖你自己的飞书多维表格 + 云端工作流 + `lark-cli`。
> 开了之后，你在飞书里随手丢一条链接，每天定时自动落到 `00-INBOX/`，再走 `#ingest` 编译入库。

## 一、它解决什么

手机上刷到好文章/视频，想存进知识库，但不想打断手头的事。
于是在飞书里建一张表，把链接丢进去 → 自动化管道每天定时抓取 → 内容出现在 INBOX → 触发 `#ingest`。

## 二、架构总览

```
飞书表格「内容收集池」
        │  （你手动粘贴链接 / 或转发消息）
        ▼
云端工作流（多维表格自动化）
        │  写入：状态=待处理、链接、标题、原文、收到时间
        ▼
lark-cli 定时读取（每日 cron）
        │  按状态筛选 → 入口链接查重 → 落地 00-INBOX/
        ▼
00-INBOX/*.md  →  #ingest 编译入库
```

## 三、你需要自建的三件东西

> ⚠️ **这一步无法自动化** —— 飞书的多维表格与云端工作流必须在飞书界面手工搭建，API 不能代建 UI 配置。

### 3.1 多维表格「内容收集池」

建一张多维表格，字段如下：

| 字段 | 类型 | 说明 |
|---|---|---|
| 状态 | 单选 | `待处理` / `已忽略` / `已入库` |
| 链接 | 文本 | 你要收集的 URL |
| 标题 | 文本 | 工作流自动填（或你手填） |
| 原文 | 多行文本 | 消息原文（转发场景用） |
| 收到时间 | 日期 | 工作流自动填 |
| 消息 ID | 文本 | 幂等去重用 |

建好后记下 **base token**（URL 里 `base/` 后面那串）与 **table id**（`tbl` 开头），写进 `config.yaml`：

```yaml
integrations:
  feishu:
    enabled: true
    cli_profile: ""          # lark-cli 的 profile 名
    base_token: "<你的 base token>"
    table_id: "<你的 table id>"
    ingest_dir: ""           # 留空 = <data_root>/00-INBOX
```

### 3.2 云端工作流（多维表格自动化）

在表格里新建自动化流程，触发条件例如「记录被创建」或「新增记录」，动作是**更新本记录字段**（状态=待处理、收到时间=当前时间）。

> 简化版：如果你愿意每次手动把状态改成 `待处理`，这一步可以省掉 —— 只是少了一点自动化。

### 3.3 lark-cli 授权

```bash
npm i -g @larksuite/cli      # 或按 lark-cli 官方文档安装
lark-cli profile add <name>  # 用你的飞书自建应用（需要 bitable 读写权限）
lark-cli profile use <name>
lark-cli profile list        # 确认 effective 身份正确
```

## 四、定时任务

管道由一个每日定时任务驱动（Agent 宿主的 cron 或系统计划任务都可以）。核心命令：

```bash
# 读表格：只看「待处理」，落盘到 .llm-wiki/tmp/
lark-cli --profile <name> base +record-list \
  --base-token "<base_token>" --table-id "<table_id>" \
  --as bot --format ndjson --limit 200 \
  --output ".llm-wiki/tmp/<table_id>_$(date +%Y%m%d_%H%M%S).ndjson" --overwrite

# 回填状态
lark-cli --profile <name> base +record-batch-update \
  --base-token "<base_token>" --table-id "<table_id>" --json @body.json
```

> ⚠️ 快照务必落在 `.llm-wiki/tmp/`，不要在仓库根目录产生散落的中间文件。

若用 Hermes 作为宿主，可以直接建一个 cron 任务：
提示词写「读 `integrations/feishu/` 文档，按其中的流程把今日待处理记录落到 INBOX，然后执行 `#ingest`」。

## 五、关键机制（踩过的坑）

| 机制 | 做法 |
|---|---|
| **增量与幂等** | 状态机驱动：只处理 `待处理`；处理完立刻回填，避免重复入库 |
| **去重** | 入口链接查重（在**抓取前**做）：抖音按 `video_id` 查账本；其他平台规范化 URL 后比对 `raw/` 里的 `source:` 字段 |
| **平台判定** | **不写平台字段** —— 平台由入库环节按域名判定回填（避免表格侧判定错误） |
| **标题双保险** | 工作流填标题 + 入库环节抓取真实标题，二者不一致时以抓取结果为准 |
| **AI 额度** | 抓取环节若用到 LLM（翻译/摘要），注意额度消耗；可只对长文启用 |

## 六、排障速查

| 症状 | 排查方向 |
|---|---|
| 表格里有记录但 INBOX 没文件 | ① 状态是否为 `待处理` ② `lark-cli profile list` 的 effective 身份对不对 ③ base/table token 是否填错 |
| 同一条链接重复入库 | 去重层是否在**抓取前**执行；抖音短链必须按 `video_id` 判重（字符串匹配不上） |
| 记录状态回填失败 | 检查应用是否有 bitable 写权限；bot 身份与 user 身份的权限不同 |
| bot 创建的资源自己打不开 | 给用户账号加 `full_access` 协作者 —— ⚠️ **open_id 是 per-app 的**，要用「资源所属应用」下的 open_id，并且**加完必须 `+member-list` 复核**（命令报 success 也可能是静默失效） |
