# 双语播客

英文播客做成一集一个网页：原文和简体中文对着声音逐行亮，能看说话人、章节，也能在 Spotify / Apple 两种样子之间换。

每天自动查 7 档节目。有新的就转写、翻译，发到 GitHub Pages。手机浏览器打开列表，加到主屏幕就能当一个小应用用。

转写和翻译跑在 GitHub Actions 上。校对、翻译、章节只调 DeepSeek 官方接口。没有自己的服务器。

播放器长什么样由 [templates/](templates/) 决定，流水线只填内容。

## 站点上有什么

打开 Pages 之后，地址是：

https://lzcyx.github.io/bilingual-podcast/

| 路径 | 内容 |
|---|---|
| `index.html` | 节目列表，可加到主屏幕 |
| `index.json` | 同一份列表的数据，给以后的 App 用 |
| `episodes/<节目>/<日期>-<标题>.html` | 这一期的播放器 |
| `covers/<节目>/<同一文件名>.jpg` | 封面 |

`index.json` 每一条有：`show_id`、`show`、`title`、`episode`、`pub_date`（ISO，带时区）、`duration_sec`、`audio_mode`（`online` 或 `offline`）、`dynamic_ads`、`html_url`、`cover_url`、`size_bytes`、`speakers`、`created_at`、`archived`。另外保留 `guid`，用来认同一期。

`gh-pages` 分支每次都是一次全新提交，不保留旧历史，避免离线音频把仓库越撑越大。

## 打开 GitHub Pages（只需做一次）

1. 打开仓库 **Settings → Pages**。
2. **Build and deployment** 里 Source 选 **Deploy from a branch**。
3. Branch 选 **gh-pages**，文件夹选 **/ (root)**，保存。

`gh-pages` 要等第一次真正发布之后才会出现。列表里还没有这个分支时，先把下面的密钥配好，等第一期跑完再回来选。

## 密钥

在 **Settings → Secrets and variables → Actions → Repository secrets** 里加。用仓库密钥，不要加在 Environment secrets 里。

| Secret | 要不要 | 做什么 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek 官方 API。模型是 `deepseek-flash`，思考模式关掉 |
| `TELEGRAM_BOT_TOKEN` | 可不填 | 有的话，每期上架后发一条消息 |
| `TELEGRAM_CHAT_ID` | 可不填 | 发给哪个聊天。两个 Telegram 密钥缺一个就整段跳过 |

密钥只放在这里。日志里不会打印出来。

Telegram 那条消息只有节目名、标题、时长、在线还是离线、这一期的链接和列表页链接，不带 HTML 文件。

## 在线版还是离线版

默认做在线版：网页大约几百 KB，播放时直接读原音频地址。

只有检测到动态插广告时才做离线版。Megaphone 会按地区和时间插入不同长度的广告，在线播放时字幕会对不齐，所以把转写用的那一份音频嵌进网页。嵌进去的是 32 kbps 单声道，一小时大约 16 MB。

现在这 6 档 IGN 节目走离线版。PlayStation 官方播客在 Libsyn 上，没有动态广告，走在线版。节目换平台之后，下一次会自己换，不用改代码。

单个文件必须小于 100 MB。整站控制在 1 GB 以内。每次发布的 Actions 摘要里会写当前站点有多大。

## 什么时候跑

| 北京时间 | GitHub 的 cron（UTC） |
|---|---|
| 每天 23:30 | `30 15 * * *` |
| 周六 08:00 | `0 0 * * 6` |

也可以到 **Actions → Podcast bilingual pipeline → Run workflow** 手动跑。同一次只跑一个工作流，避免两头一起改状态。

23:30 开跑，转写会占掉前面一段时间，翻译尽量落在北京时间凌晨的优惠时段里。费用按 DeepSeek 闲时估算：输入每百万 token ¥1，输出 ¥4，写在这一期的摘要里。这是估算，不是账单。

## 第一次不会补旧节目

`state/seen.json` 里 `bootstrapped` 一开始是 `false`。第一次成功的检查会把当时 RSS 里已有的节目都标成处理过，不转写、不翻译。之后只做新的。

想做其中某一期，必须在同一次手动运行里把节目 id 和 guid 填上。这一期不会被标成已处理，除非网页真的发布成功。

已经进了 `seen.json` 的节目，再用手动输入同一个 guid，会强制重做。

### 补做 IGN UK 第 867 期

- show id：`ignuk`
- guid：`b394abe4-b8ee-11f1-bb43-0b13ebfe32cf`

标题是 “IGN UK Podcast 867: The Big Silent Hill: Townfall Chat”。这一期是 Megaphone，会做成离线版。

查 PlayStation 官方播客最近几期的 guid（只打印，不改状态）：

```bash
python scripts/check_feeds.py --show-id opp --list 5
```

show id 填 `opp`，guid 填打印出来的那一条。这一期应该是在线版。

同一期连续失败 3 次之后记入 `state/failed.json`，不再自动重试。密钥缺失不算这一次。想重做已经放弃的一期，再用手动输入即可。

先配好 `DEEPSEEK_API_KEY`，再去点 Run workflow。没配密钥就去补某一期的话，那一期会马上停下来（不计入失败），但如果这是仓库的第一次运行，其余现有节目仍会被标成已处理。

## 加到手机主屏幕

用手机浏览器打开列表页 https://lzcyx.github.io/bilingual-podcast/ （Pages 打开之后）。

- 安卓 Chrome：菜单里选 **安装应用** 或 **添加到主屏幕**。
- iPhone Safari：点底部分享按钮，选 **添加到主屏幕**。

列表可以按节目筛选，按时间从新到旧排。每条能看到封面、标题、日期、时长，以及在线还是离线。点进去就在当前页打开这一期。已归档的只能看，点不开。

列表数据优先用网络，失败才用上次缓存。离线版的网页在你打开过一次之后会留在手机上，断网也能从主屏幕再打开，声音是嵌在文件里的。在线版断网后页面还在，声音仍然要联网。

列表最底下有 **清理缓存**，旁边是已经缓存了多少。

## 节目留多久

[shows.yaml](shows.yaml) 里的 `keep_days` 默认是 60。发布日早于这个天数的离线网页会从站点删掉，`index.json` 里那一条还在，`archived` 为 `true`。在线版很小，一直留着。

如果删完仍然超过 1 GB，会从最旧的离线版继续删，直到放得下。

增删节目只改 `shows.yaml`。`id` 用小写字母、数字和 `-`。发布过之后不要改 id，不然旧链接对不上。删掉一块就不再追这个节目，已经发出去的文件不会马上消失，要等保留天数到了才会清掉离线版。短于 10 分钟的条目和纯视频会跳过。

补做很早的离线期时，如果那一期的发布日已经超过 `keep_days`，做完会马上归档。先把 `keep_days` 调大再补。

## 模型

只使用 DeepSeek 官方接口：`https://api.deepseek.com`，模型 `deepseek-flash`，思考模式关闭。配置在 [llm.yaml](llm.yaml)。不要换成别的平台。

请求是流式的。单次最多等 10 分钟。遇到限流、服务器错误或超时，会按 2、4、8、16 秒退避，最多试 5 次。同一期最多同时翻 4 块。校对时行号必须对得上这一段里的行，对不上就重试这一段。

用量按接口和模型分开记在 `usage.jsonl` 里，摘要里有合计和费用估算。

## 流水线在做什么

1. 拉 RSS，用 guid 对比 `state/seen.json`（没有 guid 就用音频地址）。
2. 每一期一个任务，最多 4 个一起跑：下载 → 转写（faster-whisper `large-v3-turbo`）→ 说话人 → 分段 → 校对 → 翻译 → 章节 → 生成播放器 → 用手机尺寸打开检查。
3. 把网页和封面放进站点，更新列表，推到 `gh-pages`。成功之后才把这一期标成已发布。

构建任务超时 300 分钟。模型每次现下，Whisper 大约十几秒，说话人模型大约 1 秒，不进 Actions 缓存。

## 本地只跑测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pyyaml
python -m unittest tests/test_pipeline.py
```

真正做一整期还需要本机的 ffmpeg，以及 DeepSeek 的密钥。那一步很慢，也要花钱，放在 Actions 里跑。
