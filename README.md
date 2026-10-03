# 双语播客

英文播客做成一集一个网页：原文和简体中文对着声音逐行亮，能看说话人和章节。每天自动检查新节目，转写、翻译后发到 GitHub Pages。没有自己的服务器。

站点：https://lzcyx.github.io/bilingual-podcast/

播放器样式在 [templates/](templates/)。流水线只填内容。

## 什么时候跑

| 北京时间 | cron（UTC） |
|---|---|
| 每天 23:30 | `30 15 * * *` |
| 周六 08:00 | `0 0 * * 6` |

也可以到 **Actions → Podcast bilingual pipeline → Run workflow** 手动跑。同时只会有一个工作流在跑，后点的排队，不会取消正在跑的。

第一次成功检查会把当时 RSS 里已有的节目标成已处理，不补旧的。之后只做新的。短于 10 分钟和纯视频会跳过。

补做某一期时，`show id` 和 `guid` 要一起填（没有 guid 就填音频地址）。已经处理过的再填同一个 guid，会强制重做。网页发布成功之后才标成已发布。同一期连续失败 3 次就不再自动重试；缺密钥不算失败。

查某一档最近几期的 guid，不改状态：

```bash
python scripts/check_feeds.py --show-id ignuk --list 5
```

## 密钥

加在 **Settings → Secrets and variables → Actions → Repository secrets**。不要加在 Environment secrets。

| Secret | 要不要 | 作用 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek 官方接口，模型 `deepseek-flash`，思考关掉 |
| `TELEGRAM_BOT_TOKEN` | 可选 | 上架后发一条通知 |
| `TELEGRAM_CHAT_ID` | 可选 | 发给哪个聊天。两个缺一个就跳过 |

日志不打印密钥。配置在 [llm.yaml](llm.yaml)，不要换成别的平台。

## 在线版和离线版

默认在线版：网页大约几百 KB，播放时读原音频地址。

只有检测到动态插广告才做离线版，把转写用的那一份音频嵌进网页（32 kbps 单声道，一小时大约 16 MB）。现在的 IGN 节目在 Megaphone 上，一般是离线版；PlayStation 官方播客在 Libsyn 上，是在线版。换平台后下一次会自己判断。

单个文件小于 100 MB，整站控制在 1 GB 以内。

## 节目留多久

[shows.yaml](shows.yaml) 里 `keep_days` 默认 60。更老的离线网页会从站点删掉，列表里还在，标记为已归档，点不开。在线版一直留着。删完仍超过 1 GB 时，从最旧的离线版继续删。

补做已经超过保留天数的离线期，做完会马上归档。想留在站点上，先把 `keep_days` 调大。

增删节目只改 `shows.yaml`。`id` 用小写字母、数字和 `-`，发布后不要改，不然旧链接对不上。删掉一档就不再追，已经发出去的文件要等保留天数到了才清。

## 手机

用手机浏览器打开列表页，加到主屏幕即可。安卓 Chrome 选「安装应用」或「添加到主屏幕」；iPhone Safari 用分享按钮里的「添加到主屏幕」。

列表按节目筛选，从新到旧。打开过的离线版会留在手机上，断网也能听。在线版断网后页面还在，声音仍要联网。列表最底下可以清理缓存。

## 流水线

1. 拉 RSS，用 guid 对比 [state/seen.json](state/seen.json)。
2. 每一期一个任务，最多 4 个一起跑：下载、转写（faster-whisper `large-v3-turbo`）、说话人、校对、翻译、章节、生成播放器。
3. 有变化才把整站推到 `gh-pages`。这个分支每次都是全新提交。Pages 的来源是该分支的 `/ (root)`，推上去就会部署。

构建超时 300 分钟。Whisper 和说话人模型每次现下，大约十几秒，不进 Actions 缓存。翻译单次最多等 10 分钟，限流或超时时按 2、4、8、16 秒重试，最多 5 次。同一期最多同时翻 4 块。

## 本地测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pyyaml
python -m unittest tests/test_pipeline.py
```

做一整期需要本机 ffmpeg 和 DeepSeek 密钥，慢而且花钱，放在 Actions 里跑。
