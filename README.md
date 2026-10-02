# 播客 → 中英双语字幕播放器

每天检查 7 档播客的 RSS。有新节目就生成**一个**自包含 HTML 播放器（英文原文 + 简体中文，逐行高亮、说话人、章节，Spotify / Apple 两种样式），上传到 Cloudflare R2，并更新 `index.json` 给安卓 App 读。

计算在 GitHub Actions，校对 / 翻译 / 章节调用 DeepSeek，存储用 R2。没有常驻服务器。

播放器界面在 [templates/player.html](templates/player.html)，流水线只填数据，不改界面。制作步骤与 [SKILL.md](SKILL.md) 一致，脚本在 [scripts/](scripts/)。

## 你需要先配好的东西

仓库保持公开（公开仓库的 Actions 分钟数不限）。**密钥只放 GitHub Secrets**，工作流不会把它们打进日志。

在仓库 **Settings → Secrets and variables → Actions** 添加：

| Secret | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API key。模型默认 `deepseek-flash`，思考模式关闭 |
| `R2_ACCOUNT_ID` | Cloudflare 账号 ID |
| `R2_ACCESS_KEY_ID` | R2 API token 的 Access Key |
| `R2_SECRET_ACCESS_KEY` | R2 API token 的 Secret |
| `R2_BUCKET` | bucket 名称 |
| `R2_PUBLIC_BASE_URL` | 公开访问的根 URL，不要末尾斜杠，例如 `https://pub-xxxx.r2.dev` |

### 创建 R2 bucket 并公开

1. Cloudflare 控制台 → **R2** → Create bucket（名称与 `R2_BUCKET` 一致）。
2. 该 bucket → **Settings** → Public access：开启 Public Development URL，或绑定自己的域名。把得到的根 URL 填进 `R2_PUBLIC_BASE_URL`。
3. **R2 → Manage R2 API Tokens** → Create token，权限选 Object Read & Write，范围只给这个 bucket。
4. Account ID 在 Cloudflare 控制台右侧或 R2 概览页（一串十六进制）。
5. 不要把 token 写进仓库或 `llm.yaml`。

上传路径：

- `episodes/<show_id>/<YYYY-MM-DD>-<slug>.html`（长缓存）
- `covers/<show_id>/<同一 slug>.jpg`（长缓存）
- `index.json`（`Cache-Control: no-cache`，按 `pub_date` 倒序）

`index.json` 每一项：`show_id`, `guid`, `show`, `title`, `episode`, `pub_date`（ISO，带时区）, `duration_sec`, `audio_mode`（`online` 或 `offline`）, `dynamic_ads`, `html_url`, `cover_url`, `size_bytes`, `speakers`, `created_at`。

## 在线版还是离线版

`fetch_episode.py --audio-mode auto`（默认）会探测动态插广告：

- 检测到动态广告（Megaphone 等，`meta.json` 里 `dynamic_ads: true`）→ **离线版**，把转写用的那份音频嵌进 HTML。同一条音频链接在不同地区、不同时间插入的广告长度不一样，在线播放字幕会对不齐。
- 没有动态广告（PlayStation 官方播客是 Libsyn）→ **在线版**，HTML 大约 0.3 MB，播放时直接读原音频链接。

节目换平台后下一次会自动换模式，不用改代码。

## 定时

| 时间 | cron（UTC） |
|---|---|
| 每天北京时间 23:30 | `30 15 * * *` |
| 周六北京时间 08:00 | `0 0 * * 6` |

GitHub 的定时任务偶尔会晚几分钟到几十分钟。也可以在 **Actions → Podcast bilingual pipeline → Run workflow** 手动跑。

整个工作流用 `concurrency` 串行，避免并行任务同时改 `state/`。

## 第一次运行不会补旧节目

`state/seen.json` 里 `bootstrapped` 一开始是 `false`。**第一次**成功的 check 会把当时 RSS 里已有的节目标成已处理，不转写、不翻译。之后只处理新 guid。

所以要做历史中的某一期，必须在那次运行里用手动输入指定（它不会被标成已处理）。已经进了 `seen.json` 的节目，再次用手动输入指定同一个 guid 会强制重做。

### 补做 IGN UK 第 867 期

Actions → Run workflow：

- show id：`ignuk`
- guid：`b394abe4-b8ee-11f1-bb43-0b13ebfe32cf`

标题是 “IGN UK Podcast 867: The Big Silent Hill: Townfall Chat”。这一期是 Megaphone，会走离线版。

查某一档最新几期的 guid（不会改 state）：

```bash
python scripts/check_feeds.py --show-id opp --list 5
```

PlayStation 官方播客的最新一期用 `show_id=opp` 加上 `--list` 里看到的 guid。它应是在线版。

同一期连续失败 3 次后写入 `state/failed.json`，不再自动重试。密钥缺失或 R2 上传失败不算这一次（配置问题修完会再试）。想重做已放弃的一期，用上面的手动输入即可。

构建任务超时 300 分钟。Whisper `large-v3-turbo` 和说话人模型缓存在 Actions cache（`~/.cache/huggingface` 与 `~/.cache/podcast-bilingual-player/diar`）。

## 增删节目

只改 [shows.yaml](shows.yaml)。`id` 用小写字母、数字、`-`，一旦发布过就不要改，否则 R2 路径和 App 里的旧链接对不上。删掉一块即停止追更，已上传的文件不会自动删除。短于 10 分钟的条目和纯视频会跳过（`min_duration_sec`）。

## 换模型

[llm.yaml](llm.yaml) 里的 `base_url` 和 `model` 是配置项。也可以在工作流环境里设 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`（不必当 Secret，除非你不想公开供应商地址）。思考模式保持 `disabled`。

费用估算按闲时价：输入每百万 token ¥1，输出 ¥4，写在该期的 `report.json` 和 Actions summary 里。这是估算，不是账单。

## 流水线

1. **check**：拉 RSS，用 guid（没有 guid 就用音频 URL）对比 `state/seen.json`。
2. **build**（每期一个 job，最多 4 个并行）：`fetch_episode.py` → 生成 Whisper prompt / 术语表 / 人数 → `transcribe.py`（`large-v3-turbo`，CPU，`--jobs 2`）→ `diarize.py`（show notes 能数出主持人和嘉宾时用人数 + 1，多出来的一个留给广告声；否则自动）→ `segment.py` → `scripts/llm/proofread.py` → `apply_edits.py` → `tr_split.py` → `scripts/llm/translate.py`（每块立刻 `tr_check.py --only`，最多 3 次；仍失败的行标成 `［未译］`，不让整期失败）→ `scripts/llm/chapters.py`（`build.py --check`）→ `build.py`（按 config 的 `audio_mode`，不加 `--offline`）→ `test_player.py`（iPhone + Android；在线版加 `--local-audio`）。
3. **publish**：上传 HTML 和封面，更新 R2 与仓库里的 `index.json`，**上传成功后**才把 guid 写入 `state/seen.json` 并提交。

LLM 要求模型输出 JSON，并做 schema 校验（正则要能编译、编辑的行 id 必须存在、章节从 `i: 0` 递增）。`tr_check.py` 增加了可选的 `--only b01`，不写 `cues.json`；不带这个参数时行为和原来一样。

## 本地

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest tests/test_pipeline.py
sudo apt-get install -y ffmpeg   # 真正做一期时还要
python -m playwright install chromium
```

没有配密钥时不要在 Actions 里对某一期点 Run workflow，那一期会因为缺少 key 立刻停下（不计失败次数），但**同一次如果是仓库的第一次运行，其余现有节目仍会被标成已处理**。建议先把 6 个 Secret 配好，再手动补 867。
