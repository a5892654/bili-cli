# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- `bili upload activities`：按旧版分区查询推荐投稿活动，或用 `-k` 搜索；返回话题 ID、活动 ID、活动说明与可复用的 `use_with` 参数，支持翻页和 `--raw`。
- `bili upload video / edit --topic-id / --mission-id`：为新稿或已有稿件指定话题、投稿活动，支持 `--dry-run` 预览；`upload status` 显示实际保存的活动与话题 ID、名称。

### Fixed
- 普通改稿保留稿件已有的 `topic_id`、`mission_id` 与 `topic_detail`，避免修改标题、封面等内容时丢失活动关联。

## [0.5.1] - 2026-10-21

### Added
- 评论列表带上图片地址（`images`）；`bili comment images <url>` 把评论图片存到本地（长边 1280），方便多模态模型直接看图，只接受 B 站图床地址。
- `bili upload video / edit --desc-at`：简介里真正 @ 人（B 站按对方 UID 记录成 @，和网页投稿一样），最多 10 个。

### Fixed
- `bili upload edit` 改稿时原样带回简介里已有的 @（desc_v2）；之前不带，改标题等操作可能把简介里的 @ 丢掉。

## [0.5.0] - 2026-10-21

### Added
- `bili comment post --image`：评论带图片，最多 9 张（B 站不允许回复带图）。
- `bili comment emotes`：当前账号能用的 B 站表情，`-k` 搜索、`--package` 看整个表情包；表情文字直接写进正文即可。
- `--at` 可以直接填完整昵称（原来只收 UID 或 owner）。
- `bili dynamic post --at`：发动态时真正 @ 人。

### Fixed
- `bili upload edit` 拒绝修改有合作成员的联合投稿：改稿接口需要成员名单，原样提交可能出错或移除合作者。

## [0.4.0] - 2026-10-21

### Added
- `bili user info / videos`：查看任意用户的资料（粉丝、总播放、获赞、直播间）和投稿列表；可传 UID 或完整昵称，投稿可按播放 / 收藏排序、按关键词筛系列。
- `bili upload edit`：修改已投稿件的标题、简介、标签、封面、4:3 封面或 AI 声明，其余设置原样保留。
- `bili upload stats <BV>`：单稿数据，含每百次播放的互动率、完播率和平均观看时长。
- `bili me stats`：创作中心数据——播放 / 访客 / 涨粉（和上一周期对比）、播放端分布、粉丝贡献的播放占比、超过多少同类 UP、昨日涨得最多的稿件。
- `bili comment list --unreplied`：只列 UP 主还没回复过的评论。
- 全局参数 `--wait`：写操作遇到 60 秒内能恢复的本地限流时自动等待；每小时、每日上限仍直接报错。
- `bili video frames --at` 支持小数秒（如 `1:30.5`），不同小数秒的画面分开保存。
- `bili comment pin`：在自己的作品下置顶一条根评论，`--undo` 取消。
- `bili comment list` 第 1 页单独返回置顶评论（`pinned`）。
- `bili skills install` 支持 DeepSeek Harness（dsh）：检测到 `~/.dsh`（或 `$DSH_HOME`）时装进其 `skills` 目录；dsh 也会读取通用的 `~/.agents/skills`。

### Fixed
- `bili comment check` 把置顶评论误报为“找不到”（B 站不把置顶评论放进普通列表）；现在会识别并标出 `pinned`，并往后多查两页。

## [0.3.0] - 2026-10-20

### Added
- `bili upload video --cover43`：单独上传手机端推荐流使用的 4:3 封面。

## [0.2.0] - 2026-10-20

### Added
- 发布到 PyPI，包名 `bili-agent`（`bili-cli` 已被占用），安装只要 `uv tool install bili-agent`；命令仍是 `bili`。打 `v*` 标签后自动发布。
- `bili skills install / uninstall / status / show`：把 Agent Skill 装进本机的 Claude Code、Codex、Cursor、Gemini CLI、Copilot、OpenClaw、Qwen Code 等 AI Agent（只写入已检测到的 Agent，不覆盖用户自己的同名技能）。
- 一行安装脚本 `scripts/install.sh`、`scripts/install.ps1`，以及给 Agent 看的安装指南 `docs/install.md`。
- MCP 模式：`bili mcp serve`（17 个工具，写操作带非只读标注，行为和护栏与命令行完全一致）；`bili mcp install / uninstall` 注册到 Claude Desktop、Claude Code、Cursor、VS Code、Codex、Gemini CLI（先备份，只改自己的条目）。安装 `bili-agent[mcp]`。
- `DISCLAIMER.md`、`SECURITY.md`、`CONTRIBUTING.md`。
- `bili video like / coin / fav / triple / relation`、`bili me favs`：点赞、投币、收藏到指定收藏夹、一键三连、查看我和视频的关系。投币必须显式指定个数，本地每 24 小时最多 5 个；对没看过的视频操作会提示。
- Logo、Banner，README 重写。

## [0.1.0] - 2026-10-20

首个版本。

- 扫码登录（两步走，适合 Agent），凭证本地保存。
- 看视频：信息、字幕、弹幕、AI 总结、分段章节、画面抽帧与总览图、语音转写（本地 Qwen3-ASR GPU/CPU，或云端 OpenAI 兼容 / 阿里云 / 腾讯云），`video watch` 一次拿全。
- 刷视频：首页推荐、热搜、综合热门、每周必看、入站必刷、排行榜，`--unseen` 跳过看过的。
- 互动：评论读写、真正的 @（`at_name_to_mid`）、评论可见性检查、消息收件箱（回复 / @ / 赞 / 系统通知 / 私信）、私信收发（带防骚扰白名单）。
- 创作：动态、投稿（默认“含AI生成内容”声明、自动预测分区、定时、仅自己可见）、稿件状态、分段章节、互动弹幕（投票、评分、下期预告、关联视频、关注 / 三连按钮）、个人资料。
- 护栏：所有写操作本地限流 + 审计日志，`--dry-run` 预览，统一 JSON 输出与错误码。
