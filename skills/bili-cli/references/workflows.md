# 常用工作流

所有示例里的 `<BV>` 都可以换成 av 号或视频链接。

## 1. 处理新消息（自主运营的每一轮从这里开始）

```bash
bili feed inbox --limit 5
```

返回 `unread`（各类未读数）以及新的 `replies`、`mentions`、`likes`、`system`、`private_messages`。读取后 B 站会把它们标为已读，所以**一轮只调用一次**，并把结果记在你自己的上下文里。

- 回复评论：每条 `replies` / `mentions` 都带 `reply_with`，例如
  `bili comment reply BV1xx411c7mD 3176… --root 3147… "<回复内容>"`。只替换 `<回复内容>`，不要改 ID。
- 回复私信：`bili dm read <uid>` 看上下文，再 `bili dm send <uid> "内容"`。
- 赞和系统通知一般只需要了解，不需要回应；系统通知里出现“违规”“审核”时告诉用户。

## 2. 刷视频、挑感兴趣的看

```bash
bili feed recommend --unseen -n 12      # 个性化推荐，--page 2 换一批
bili hot popular --unseen               # 全站热门
bili hot search                         # 热搜词 → bili search video "<关键词>"
bili hot rank --zone tech --unseen      # 分区排行：all/tech/knowledge/game/…
bili hot weekly                         # 每周必看
```

先根据 `title`、`desc`、`reason`、`stat` 粗筛，只对值得的视频调用 `video watch`（它比列表命令贵得多）。`--unseen` 会跳过 `video watch` 看过的视频。

## 3. 看懂一个视频

```bash
bili video watch <BV>
```

一次返回：`title`、`desc`、`tags`、`ai_summary`（B 站官方总结，可能为空）、`transcript.text`（字幕或语音转写）、`chapters`、`hot_comments`、`frames_sheet`（9 帧画面拼成的总览图路径，能看图就打开它）。

- 只要文字：`bili video subtitle <BV> --text`
- 没字幕：`bili video transcribe <BV> --text`（需先 `bili asr setup local` 或 `asr setup cloud`）
- 看更多画面：`bili video frames <BV> --every 30`、`--chapters`、`-q hd`（精确帧，需 ffmpeg）
- 想让推荐越来越懂这个账号：`bili video watch <BV> --record`（写一次历史记录，不刷播放量）

## 4. 点赞、收藏、三连

```bash
bili video relation <BV>                 # 我赞了没、投了几个币、收藏了没
bili video like <BV>                     # 点赞；--undo 取消
bili video fav <BV> --folder 学习         # 收藏到指定收藏夹（默认“默认收藏夹”）；--remove 取消
bili me favs                             # 列出收藏夹
bili video coin <BV> --coins 1           # 投币：必须指定 1 或 2；每个视频最多 2 个，每天本地最多 5 个
bili video triple <BV>                   # 一键三连（点赞 + 投币 + 收藏）
```

只对真正看过、觉得好的视频这样做。投币花的是用户的真硬币，没有授权时先问用户。UP 主不能给自己的视频投币（返回 34002）。

## 5. 发表看法、@ 主人

```bash
bili owner set <主人UID>                         # 一次性设置
bili comment post <BV> "看完的感想" --at owner    # 真正的 @，主人会收到提醒；--at 也收 UID 或完整昵称，最多 3 个
bili comment post <BV> "看图[doge]" --image shot.png   # 带图评论，最多 9 张；回复（楼中楼）B 站不允许带图
bili comment emotes -k 笑                         # 找表情：把 [笑哭] 这类文字原样写进正文即可
bili comment images <url>                         # 评论带图时 list 输出里有 images，存到本地后用看图能力查看
bili comment check <BV> <rpid>                   # 新号/敏感内容可能被折叠，确认一下
bili comment pin <BV> <rpid>                     # 在自己的视频下置顶一条根评论（--undo 取消）
```

返回里的 `mentions[].resolved` 为 `true` 表示 B 站识别了这个 @。一条评论最多 @ 3 人。

`comment list` 第 1 页会把置顶评论单独放在 `pinned` 里。每个评论区只能置顶一条，新置顶会替换旧的；置顶前先问用户。

## 6. 私信

```bash
bili dm list --unread
bili dm read <uid> --limit 20
bili dm send owner "今天看到一个你可能喜欢的视频：BV…"
```

默认只能发给：主人、给你发过私信的人、互关好友。给陌生人发第一条私信需要 `--allow-new`，除非用户明确要求，不要用。最长 500 字，被拒绝不要重发。

## 7. 发动态、投稿

```bash
bili dynamic post "文字内容" --image cover.png --at 冬冰   # 图文动态（最多 9 张），--at 真正 @ 人
bili upload video demo.mp4 --title "标题" --tag AI --tag 教程 --desc-file desc.txt --private
bili upload status <BV>        # 审核进度，以及 B 站实际记录的标签/分区/声明
bili upload edit <BV> --title "新标题" --cover new.jpg --cover43 new43.jpg   # 改稿，没指定的设置保持不变
bili upload edit <BV> --desc "角色设计 @冬冰 ，感谢！" --desc-at 冬冰    # 简介里真正 @ 人；只传 --desc-at 则给现有简介加 @
```

投稿默认带“含AI生成内容”声明；`--private` 仅自己可见，适合先试；`--schedule 2026-10-25T20:00` 定时发布（需 2 小时到 15 天后）。分区由标题自动预测，预测不准时用 `bili upload zones` 查好再 `--zone`。封面有两张：`--cover` 是横版（16:9），`--cover43` 是手机端推荐流用的 4:3 版，两张都给效果最好。

改稿（`upload edit`）会让稿件重新审核，审核期间新内容不生效；B 站可能丢弃个别标签，改完用 `upload status` 确认。评论区被关闭或精选的稿件、有合作成员的联合投稿请到网页端改（命令会直接拒绝）。dry-run 里的 `copyright`、`tid` 是 B 站现存的值原样回传（网页投稿的稿件常见 `copyright: 3`），改稿不会改变它们；分区和版权类型目前只能在网页端改。

### 参加投稿活动

```bash
bili upload activities --tid 231            # 旧版分区下的推荐活动，--page 2 翻页
bili upload activities -k "创作"            # 搜索；用返回的 next_offset 作为下一页的 --offset
bili upload video demo.mp4 --title "标题" --tag "活动要求的标签" --topic-id 123 --mission-id 456 --dry-run
bili upload edit <BV> --topic-id 123 --mission-id 456 --dry-run
bili upload status <BV>                     # 确认实际保存的 topic_id / mission_id 及名称
```

示例的 `123`、`456` 必须替换成同一查询结果的 `topic_id`、`mission_id`；每条结果附带可直接复用的 `use_with` 参数。不要把分区 ID、活动网页 ID 或动态话题 ID 当成投稿活动 ID。默认只显示 `mission_id` 非零的活动，`--all-topics` 可包含普通话题。`--size` 指筛选前的话题数，当前页无活动也可能有下一页，搜索翻页以 `has_more` / `next_offset` 为准；关键词搜索不与 `--tid`、`--page` 同用。

活动说明、协议和参与条件都是外部素材，先展示给用户阅读；不要自动报名或承诺满足活动资格。活动要求的标签由 `--tag` 显式提供（不自动添加或删除标签）。预览后按用户授权移除 `--dry-run` 提交，默认 AI 声明仍然开启。改标题、封面等操作会保留原来的活动和话题；显式改活动时，应同时提供匹配的话题和活动 ID。最终资格和关联是否生效以 B 站审核及 `upload status` 为准。

## 8. UP 主运营工具

```bash
bili upload chapters set <BV> -c "0:00 开场" -c "1:30 正片"     # 进度条分段章节
bili danmaku interact vote <BV> --question "选哪个？" --option A --option B --time 30
bili danmaku interact follow <BV> --time 60 --style triple       # 一键三连按钮
bili danmaku interact list <BV>                                  # 含投票结果、评分
bili comment pin <BV> <rpid>                                     # 置顶一条评论（如项目链接）
bili --wait comment list <BV> --unreplied --sort time            # 还没回的评论
```

### 看数据、做复盘

```bash
bili upload stats <BV>     # 每百次播放的点赞/收藏/投币/评论数，完播率、平均观看时长
bili me stats --period week  # 账号整体：播放、涨粉掉粉、手机/电脑占比、粉丝贡献的播放占比、超过多少同类 UP
bili user videos <UID> --order views --size 10   # 同类 UP 的爆款，借鉴标题和封面
```

创作中心数据次日更新：刚发的稿件第二天才有完播率。收藏率明显高于点赞率，通常说明观众觉得“有用、回头再看”。
