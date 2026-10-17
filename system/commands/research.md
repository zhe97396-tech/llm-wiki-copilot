---
name: research
description: "主动搜索补充知识：拆解主题→多平台搜索→筛选→入库→编译"
triggers_en: ["research", "调研", "search web", "补充知识"]
category: retrieval
---

# 🔬 主动搜索补充知识：`#research` 或 `#调研`

**触发时机**：用户发送 `#research [主题]` 或 `#调研 [主题]`。

**执行动作**：
1. **判断模式**：
   - **补充模式**：如果用户指定了已有概念/文章（如 `#research MCP协议 补充细节`），搜索结果直接补充到对应文件中，不经过 INBOX
   - **新增模式**（默认）：搜索知识库里没有的新内容，存到 `00-INBOX/` 后自动编译
   - **自动判断**：如果用户没明确指定，先搜索知识库——匹配到概念页则补充概念页，匹配到总结页则补充总结页，都没匹配到则走新增模式
2. **拆解主题**：将用户给的主题拆解为 3-5 个具体搜索查询。例如：
   - `#research 元宇宙教育` → ["元宇宙教育应用案例", "VR教育技术现状", "元宇宙教育政策"]
3. **多平台搜索**：根据主题判断适合的平台，按路由策略选择搜索工具：

   ### 搜索路由策略

   > **核心逻辑**：OpenCLI 做平台搜索+读取，Chubby Skills 做 URL 抓取入库，web_search 做中文网页兜底。

   **环节一：搜索（找内容）**

   | 搜索场景 | 搜索工具 | 命令 | 登录状态 |
   |----------|----------|------|---------|
   | B站视频 | OpenCLI | `opencli bilibili search "关键词"` | ✅ 已登录 |
   | YouTube视频 | OpenCLI | `opencli youtube search "关键词"` | ✅ 已登录 |
   | Twitter/X | OpenCLI | `opencli twitter search "关键词"` | ✅ 已登录 |
   | Reddit讨论 | OpenCLI | `opencli reddit search "关键词"` | ✅ 已登录 |
   | GitHub项目 | OpenCLI | `opencli github-trending repos` | ✅ 已登录 |
   | 小红书用户笔记 | OpenCLI | `opencli xiaohongshu search "关键词"` | ✅ 已登录 |
   | 抖音视频 | OpenCLI（无search） | 抖音 adapter 只有 `opencli douyin whoami`（需 cookie），**无 search**；搜抖音用 web_search，或用户直接发链接入库 | ❌ 不可用 |
   | 公众号文章 | OpenCLI | `opencli weixin search "关键词"` | ✅ 无需登录（搜狗微信搜索） |
   | 公众号文章入库 | Chubby Skills | `python <config.integrations.chubby.path>/tools/chubby.py ingest "<URL>"` | 后端抓取，无需浏览器 |
| 中文网页/博客/新闻 | web_search | 宿主提供（有则用，无则跳过） | 无需登录 |
   | 任意网页全文 | Jina Reader | `curl https://r.jina.ai/<URL>` | 无需登录 |

   **环节二：入库（转Markdown）**

   | 内容来源 | 入库工具 | 说明 |
   |----------|----------|------|
   | 国内平台链接（B站/公众号/X/知乎/小红书/播客/TikTok） | Chubby Skills | `python <config.integrations.chubby.path>/tools/chubby.py ingest "<URL>"` → 直接输出 Markdown |
   | 抖音链接 | 独立转录脚本 | `python <config.integrations.douyin.python> <config.integrations.douyin.transcribe_script> "<URL>"` → fetch_video_meta（playwright 渲染 + 拦截 aweme/detail API，2026-08-17 起，iesdouyin 分享页改版已失效）→ curl下载 → ffmpeg → SenseVoice → **规则纠错词表(A1，Step 5.5 确定性兜底)** → **patch式 LLM 整理(A2，默认；--full-correct 可强制旧全文)** → MD ｜ **多条批量**：`python <config.integrations.douyin.transcribe_script 同目录>/douyin_batch.py --file 链接清单.txt`（模型一次加载N条复用，可从清单文件批量喂入）|
   | OpenCLI 读到的文本 | 直接写入 | 已是文本，直接存入 `00-INBOX/` |
   | Jina Reader 提取的网页 | 直接写入 | 已是文本，直接存入 `00-INBOX/` |
   | web_search 找到的网页 | Jina Reader 提取 | 先用 Jina 读全文，再存入 `00-INBOX/` |

   ### 路由判断逻辑

   - 用户明确指定平台 → 用对应 OpenCLI adapter（如"搜一下 Reddit 上..."→ `opencli reddit search`）
   - 主题偏国内视频 → 优先 B站/抖音/小红书
   - 主题偏海外社区 → 优先 Reddit/Twitter/YouTube
   - 主题偏技术工具 → 优先 GitHub trending + web_search
   - 主题偏中文文章 → 优先 web_search
   - 不确定 → web_search + OpenCLI 双搜
   - 搜索结果中有国内平台具体 URL → 用 Chubby Skills 转成 Markdown 入库
   - 搜索结果中有海外平台 URL → 用 OpenCLI 读取后直接写入

   ### 搜索优先级（按平台特性，非主题）

   > 平台本身是聚合的，按内容形态选平台，而非按主题排优先级。

   | 主力 | 备选 | 适用场景 | 理由 |
   |------|------|----------|------|
   | 小红书 | 抖音 | 中文实操经验、工具推荐、涨粉变现技巧 | 图文快读，搜索质量高 |
   | 公众号 | web_search | 中文深度长文、方法论、行业分析 | 体系化内容，搜狗微信搜索 |
   | B站 | YouTube | 视频教程、体系化课程、深度拆解 | 长视频，课程级内容 |
   | Reddit | Twitter/X | 海外社区讨论、真实经验、技术话题 | 社区驱动，多视角 |
   | GitHub | — | 开源项目/AI工具发现 | 独此一家 |

   **选择逻辑**：
   - 中文实操经验 → 小红书（备选抖音）
   - 中文深度文章 → 公众号（备选web_search）
   - 视频教程/课程 → B站（备选YouTube）
   - 海外讨论/技术 → Reddit（备选Twitter/X）
   - 找工具/开源项目 → GitHub
   - 不确定 → 主力+备选双搜

4. **筛选结果**：
   - 去除低质量/营销内容
   - 优先选择：学术论文、深度长文、权威媒体、社区高质量讨论
   - 每个查询保留 Top-3 高质量结果
5. **展示摘要**：向用户展示筛选后的结果列表，每条包含：
   - 标题 + 来源 + 平台
   - 一句话摘要
   - 相关性评分（高/中/低）
6. **用户确认入库**：询问用户哪些结果要入库。用户确认后：
   - **补充模式**：将新信息追加到目标文件底部 `## 📝 补充调研（日期）` 区块，不改变 status。同时处理：
     - **新概念**：如果补充内容中出现了知识库里没有的新概念，在 `wiki/concepts/` 新建概念页，补充内容中用双链指向
     - **新双链**：补充内容中的双链直接写入，指向已有页面则建链，指向不存在的页面则留为断链（health.py 会检测）
     - **新标签**：追加到文件 YAML tags 数组
     - **更新索引**：如有新概念，更新 `wiki/index.md`
   - **新增模式**：将选中的文章内容保存到 `00-INBOX/`，触发编译流程（自动执行 PROMPT.md 的 Step 0-4）
7. **记录日志**：在 `wiki_log.md` 追加 `[- [{{当前精确时间}}] 🔬 RESEARCH: 调研了 [主题]，入库 [N] 篇文章]`

**降级**：如果搜索工具不可用，提示用户手动搜索后将文章放入 `00-INBOX/`。

### OpenCLI 常用命令速查

```bash
# B站
opencli bilibili search "关键词"
opencli bilibili video <URL或BV号>
opencli bilibili subtitle <URL或BV号>
opencli bilibili dynamic  # 用户动态
opencli bilibili ranking  # 排行榜

# YouTube
opencli youtube search "关键词"
opencli youtube video <URL>
opencli youtube transcript <URL>
opencli youtube channel <频道URL>
opencli youtube comments <URL>

# Twitter/X
opencli twitter search "关键词"
opencli twitter article <文章URL>
opencli twitter bookmarks  # 我的收藏

# Reddit
opencli reddit search "关键词"
opencli reddit read <帖子URL>
opencli reddit hot  # 热门
opencli reddit subreddit <子版块名>

# 小红书
opencli xiaohongshu search "关键词"  # 搜索笔记
opencli xiaohongshu note <笔记URL>  # 读取笔记详情
# python <config.integrations.chubby.path>/xiaohongshu-ingest/scripts/fetch_note.py "<小红书URL>"  # 入库图文/视频（2026-08-21 起可用，cookie 自动读 asr-env/scripts/.xhs_cookie）

# GitHub
opencli github-trending repos --language <语言> --since <daily/weekly/monthly>

# 公众号
opencli weixin search "关键词"  # 搜狗微信搜索
# python <config.integrations.chubby.path>/tools/chubby.py ingest "<公众号URL>"  # 入库（后端抓取）

# 抖音（无 OpenCLI search；搜抖音用 web_search，入库走转录脚本）
# python <config.integrations.douyin.python> <config.integrations.douyin.transcribe_script> "<抖音URL>"  # 入库转录（2026-08-17 起内部走 playwright 拦截 aweme/detail API）

# 微博：不使用

# 通用网页读取
opencli browser <session> open <URL>
opencli browser <session> extract  # 提取页面Markdown
```

### Chubby Skills 常用命令速查

```bash
# 单链接抓取→Markdown（自动识别平台）
python <config.integrations.chubby.path>/tools/chubby.py ingest "<URL>"

# 批量抓取
python <config.integrations.chubby.path>/tools/chubby.py run

# 查看状态
python <config.integrations.chubby.path>/tools/chubby.py status --latest
```

---
