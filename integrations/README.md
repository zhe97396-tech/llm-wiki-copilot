# 可选集成总览

> **默认全部关闭。** 核心链路（编译 → 检索 → 带读 → 沉淀）不需要这里的任何一项。
> 只有当你需要「把外部内容自动采集进知识库」时，才按需开启。

## 一句话原则

> **安装可自动，授权必须人工。**

每个集成都分两步：① 装工具（可脚本化）② 授权登录（只能你本人做）。

## 四个集成

| 集成 | 用途 | 依赖 | 需要你手工做的 |
|---|---|---|---|
| **douyin** | 抖音视频 → 文字稿入库 | `funasr` + `torch` + SenseVoice 模型 + `ffmpeg`（约 2-4 GB） | 装 ffmpeg |
| **chubby** | B站/公众号/X/小红书等平台抓取与转录 | 第三方仓库（MIT）+ `DEEPSEEK_API_KEY` / `XHS_COOKIE` | 平台登录态 |
| **opencli** | 8 平台搜索（话题调研、对标查找） | 第三方 npm 包（Apache-2.0） | 各平台扫码登录 |
| **feishu** | 飞书表格 → 每日自动落入 INBOX | `lark-cli` + 你自建的多维表格与工作流 | **必须手工建表**（见下） |
| **github-stars** | 同步你的 GitHub 星标项目入库 | 脚本已内置（`scripts/github_stars_sync.py`） | GitHub 用户名 |

## 第三方工具的安装方式（我们**不**把它们的代码打包进来）

这么做是为了：不承担维护别人的活跃项目、不丢上游更新、仓库里零第三方代码。

```bash
# Chubby Skills —— MIT，作者 chubbyguan
git clone https://github.com/chubbyguan/chubbyskills.git
cd chubbyskills && bash setup.sh video    # 分档：light / video / podcast / wechat / all / doctor

# OpenCLI —— Apache-2.0，作者 jackwener
npm i -g @jackwener/opencli
opencli --version
```

装好后把路径写进 `config.yaml` 的 `integrations` 段（见 `config.example.yaml`）。

## 依赖与致谢

| 组件 | 作者 | 许可 | 集成方式 |
|---|---|---|---|
| zvec | Alibaba | Apache-2.0 | pip 依赖 |
| fastembed | Qdrant | Apache-2.0 | pip 依赖 |
| markitdown | Microsoft | MIT | pip 依赖 |
| jieba | fxsjy | MIT | pip 依赖 |
| Chubby Skills | chubbyguan | MIT | 引导安装 |
| OpenCLI | jackwener | Apache-2.0 | 引导安装 |

## 三个永远无法自动化的事

| 事项 | 为什么 |
|---|---|
| 平台登录态 | 扫码/授权属于你的账号，任何脚本都不能替你登录 |
| 飞书云端工作流 | 必须在飞书界面手工创建，API 不能代建 UI 配置 |
| 各类密钥 | LLM API key / GitHub token 只能由你提供 |
