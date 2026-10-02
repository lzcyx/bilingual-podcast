---
name: podcast-bilingual-player
description: Use this when the user wants to turn a podcast episode (an episode web page, RSS item or mp3 URL) into a bilingual synced-subtitle mobile HTML player — source transcript plus a translation (English→Chinese by default) highlighted line-by-line with the audio, with speaker labels, chapters, an EN/中 toggle, tap-to-seek and a switchable Spotify/Apple look, packaged as one self-contained HTML file that streams the episode audio online (default) or embeds it for offline use (the default for feeds with dynamic ad insertion, e.g. Megaphone/Acast/ART19, where streamed ads differ per listener).
---

# Podcast → bilingual synced-subtitle player

Output: ONE self-contained `.html` per build (phone-first, works on iPhone Safari / Android Chrome).
Both looks ship in the same file and can be toggled live (no reload, playback position kept); the choice is
remembered in localStorage. Config `variant` / `build.py --variant` only sets the **default** style:

- **S** (default) — Spotify "Now Playing": cover-colour gradient header and subtitle surface, current line white,
  upcoming lines `rgba(0,0,0,.55)`, played lines 65 % white, `#121212` player bar, white round play button, green
  only on the current-chapter equaliser.
- **B** — Apple Podcasts flat colour: solid cover-derived base, darkened bar/sheet.

The theme colour is extracted from the cover art automatically. Features: line highlight + auto-follow, tap a line
to seek, chapter bar with segmented progress and scrub tooltip, chapter sheet, prev/next chapter, EN/中 cycle
(both → target first → source only → target only), speed, large text, collapsible info strip (remembered),
resume position (localStorage), lock-screen media controls, **speaker names at the start of each turn** (subtle
per-speaker colour pill; show/hide remembered; degrades gracefully when cues have no `spk`).

- **Online** (default for normal feeds, `"audio_mode": "online"`): streams `audio_url`; file ≈ 0.3 MB.
- **Offline** (`"audio_mode": "offline"` or `build.py --offline`): audio embedded as 40 kbps mono mp3
  (≈ 18 MB of mp3 per hour → ≈ 24 MB of HTML per hour after base64; a 58-min episode = 23.5 MB); works with no
  network. Still one file with both styles. **Default when the feed uses dynamic ad insertion** (see below).

Each `build.py` run writes exactly one file. Build offline when the user asks for it / configures it, **or when
`dynamic_ads` is true** (fetch_episode.py then already drafts `"audio_mode": "offline"`).

### Dynamic ad insertion (DAI) → offline by default
Many podcast hosts (Megaphone, ART19, Acast, Omny Studio, Simplecast/AdsWizz, Spreaker, Audioboom, iHeart, …,
often behind podtrac / pscrb.fm / mgln.ai / chtbl redirect prefixes) stitch ads into the mp3 **per request** — by
region, time, device and listener. The transcript and every cue time come from the ONE file we downloaded (e.g.
IGN UK 867: ≈ 2 min of pre-roll + a 16 s mid-roll). A listener streaming the same `audio_url` elsewhere (the user
is in China) gets different/longer/no ads, so the audio shifts against the subtitles after every ad break and
the sync is wrong for most of the episode. Therefore:
- `fetch_episode.py` probes `audio_url` (follows redirects, checks every hop + the final host, Megaphone's
  `has-ads: 1` header, "megaphone.fm/adchoices"-style show notes) via `dai.py`, records `"dynamic_ads": true` and
  the redirect chain in `meta.json`, writes `"dynamic_ads": true, "audio_mode": "offline"` into
  `config.draft.json` and prints a warning. Force with `--audio-mode online|offline`.
- `build.py` prints a warning when an ONLINE build is made for a `dynamic_ads` config (or, if the key is absent,
  when `audio_url` is on a known DAI host). Always embed the **transcribed** `audio_file`; a fresh download of a
  DAI URL has a different ad set (build.py warns if it has to re-download).
- Check by hand: `python $S/dai.py "<audio url>"` → JSON with `dynamic_ads`, `providers`, `chain`, `reasons`.
- Online is still possible for DAI feeds (`--online`) but only stays in sync for listeners who happen to get the
  same ads; tell the user.

## Prerequisites (fresh Linux; also fine on Windows / Docker later)

```bash
sudo apt-get install -y ffmpeg                      # ffmpeg + ffprobe on PATH
python3 -m venv .venv && . .venv/bin/activate       # Windows: py -3 -m venv .venv & .venv\Scripts\activate
pip install -r <skill>/requirements.txt             # faster-whisper, numpy, Pillow, sherpa-onnx, playwright
python -m playwright install chromium               # for test_player.py (or pass --chrome /usr/bin/google-chrome)
```
The Whisper model `large-v3-turbo` (CTranslate2, ≈1.6 GB) downloads from Hugging Face on first use into
`~/.cache/huggingface` (use `--model-dir` to change). Needs ≈2 GB RAM per transcription job.
Speaker-diarization models (pyannote segmentation ONNX + NeMo Titanet embedding, both **public** GitHub
releases — no Hugging Face token) download once into `~/.cache/podcast-bilingual-player/diar/`
(override with `diarize.py --model-dir`). `S=<skill>/scripts` below. All scripts print `--help`.
All script I/O uses `encoding='utf-8'`; no `shell=True`; paths are cross-platform.

## Time expectations (CPU only, 8 vCPU box)

| step | time |
|---|---|
| fetch audio/metadata | 1–2 min |
| transcription, large-v3-turbo float32, 2 jobs × 4 threads | ≈ 20–25 min for 56 min of audio (60 s clip ≈ 35–45 s incl. model load) |
| diarization (sherpa-onnx, CPU) | ≈ 25–30 s per 5 min of audio (≈ 3–4 min for a 1 h episode) |
| segment / apply edits / split / check / build | seconds each |
| proofreading + speaker naming (agent) | 15–40 min |
| translation (agent, ~100 lines per block, ~9–10 blocks per hour of audio) | the longest agent step |
| chapters + build + test | ~5 min |

Start transcription in the background and read the episode page / build the glossary while it runs.
Diarization can run in parallel with (or right after) transcription — it only needs the audio / 16 kHz wav.

## Recipe

### 1. Audio, metadata, cover, page text
```bash
python $S/fetch_episode.py --page "<episode page URL>" [--rss "<feed URL>"] --workdir work
# or: --mp3 "<direct mp3 URL>" --rss "<feed URL>"
```
Writes `work/episode.mp3`, `work/cover.*`, `work/page.txt` (show notes: the source of truth for proper
nouns **and host/guest names**), `work/meta.json`, `work/config.draft.json`. It prints the audio redirect chain;
if it warns **dynamic ad insertion detected**, keep the draft's `"audio_mode": "offline"` (see above) and never
re-download the mp3 after transcribing. Review the draft: `show` may come
out as the site name, the title may need trimming, the cover may be the show art rather than the episode art.
`audio_url` must be a directly playable mp3 URL (a CDN URL with redirects is fine). If the page links to
articles about the topics discussed, skim them for spellings too.

Write `work/prompt.txt`: 1–3 natural sentences naming the show, hosts, guests and key titles (see
`examples/prompt.example.txt`). It steers Whisper's spelling. Start `work/glossary.txt`
(`examples/glossary.example.txt`): source term → target term, with official Chinese titles where they exist.

### 2. Transcribe (chunked, word timestamps)
```bash
python $S/transcribe.py work/episode.mp3 --workdir work --prompt-file work/prompt.txt   # add --lang for non-English
```
Cuts ~120 s chunks at the quietest point near each boundary, runs `--jobs` worker processes
(default cpu/4, each with cpu/jobs threads), writes `work/chunks/kNN.json`, then merges them into
`work/words.json`. Resumable: re-run to continue; delete a `kNN.json` to redo that chunk. Use large-v3-turbo:
smaller models skipped chunks or garbled text on CPU. `--compute-type int8` is faster but worse.
Warning sign: `WARNING empty chunks`, or chunks with very few words → delete them and re-run.
Quick sanity test: `--start 600 --duration 60 --workdir clip`.

### 3. Diarize (speaker labels)
```bash
python $S/diarize.py --workdir work --num-speakers 4          # preferred when the cast size is known
# or auto: python $S/diarize.py --workdir work                 # uses --threshold 0.85 + merges tiny clusters
```
Uses sherpa-onnx `OfflineSpeakerDiarization` (public pyannote-segmentation-3.0 ONNX + `nemo_en_titanet_small`
embedding — **no Hugging Face token / gated model**). Writes `work/diarization.json`
`[{"s","e","spk":0}, …]` and `work/diarization_meta.json`. Prefer `--num-speakers` when page.txt lists the
cast; otherwise tune `--threshold` (higher → fewer speakers). Clusters with less than
`--min-speaker-seconds` (default 8) of speech are merged into neighbours (laughter / music / crosstalk).

### 4. Segment into subtitle lines
```bash
python $S/segment.py --workdir work        # -> work/lines_raw.json + lines_raw.tsv (id, m:ss, spk, text)
```
Breaks on pauses / sentence ends **and on speaker changes**. Each line carries `"spk": "S0"` when
diarization is present. Without `diarization.json` the pipeline still works (no `spk` field) — old builds
remain valid.

### 5. Proofread + name speakers (agent)
Read `lines_raw.tsv` against `page.txt`/glossary. Fix systematic mis-hearings with regexes in
`work/fixes.json` (`examples/fixes.example.json`, `[pattern, replacement]` pairs, applied to every line not edited
by hand). Fix individual lines in `work/edits.json`, keyed by line id:
```json
{"04-007": {"en": "full corrected text"},
 "08-016": {"split": [[1, "eh?", "^"], [0, "During the day you are fully human."]]},
 "11-030": {"drop": true},
 "12-001": {"merge_prev": true},
 "12-004": {"spk": "S1"},
 "15-002": {"split": [[6, "first part.", "", "S0"], [0, "second speaker continues.", "", "S1"]]}}
```
`split` = word counts per part (0 = rest; counts must cover the line). A part marked `"^"` is appended to the
previous line. An optional 4th element (or `"spk"` in a dict part) sets the speaker for that part.
Use split where one line holds two speakers or two sentences that should be separate lines.
Drop hallucinations (e.g. repeated "Thank you." in silence, music-only lines).

**Map raw speaker IDs to real names.** From `page.txt` (The Cast / hosts / guests) and the transcript
(self-introductions, "thanks X", "I'm looking at Y"), write `work/speakers.json`:
```json
{"S0": {"name": "Sid Shuman", "color": "#7eb8ff"},
 "S1": {"name": "Tim Turi"},
 "S2": {"name": "O'Dell Harmon Jr.", "color": "#7ddea2"}}
```
(`color` optional — the player falls back to a fixed palette.) Override wrong-speaker lines with
`{"id": {"spk": "S…"}}` in edits.json; expect a handful of boundary errors even with a good `--num-speakers`
hint (see quality checklist).
```bash
python $S/apply_edits.py --workdir work    # -> work/lines.json + lines.tsv (i, id, m:ss, spk, text); re-run after every change
```
Finish structural edits (split/merge/drop) BEFORE translating: line indexes `i` are fixed from here on.

### 6. Translate (agent, block by block, ｜ protocol)
```bash
python $S/tr_split.py --workdir work [--lang zh]   # -> work/tr/b01.src.txt …
```
Each source line is `key<TAB>text`. A key is a line index (`17`) or a range (`12-14`) for lines that form one
sentence, joined with ` | `. Groups never cross a speaker boundary. When speakers are known, a
`# speaker: Name (S0)` comment precedes each turn so the translator knows who is talking. For every
`bNN.src.txt` write `bNN.zh.txt` (`bNN.<lang>.txt`) with the SAME keys:
```
8-9	我们都在这儿陪你。今天我们要好好聊聊眼下扎堆推出的这一大波好游戏，以及｜我们玩了多少。我们玩了好多。
21-22	对。嗯，我｜觉得他们可以乖乖跟上 fall 的叫法嘛。哦，妙啊。
```
Rules:
- Translate each group as one natural sentence in context (read the whole block and the `# context:` /
  `# speaker:` lines first), then put exactly one full-width `｜` where each ` | ` break is, so each part lines
  up with its English line and its timing. A group of n lines has n parts and n−1 `｜`. No part may be empty.
  Reorder words across the break if needed, but keep each part roughly aligned with what is said in that line.
- Spoken, natural target language; keep fillers light; no summarising or dropping content.
- Official Chinese titles for games, films, shows and products where they exist (check the glossary and the web),
  in 《》; otherwise keep the original title in 《》. Host and guest names stay in English. Brand names such as
  PlayStation, DualSense and PS5 stay as is.
- Write one block file per step and check as you go:
```bash
python $S/tr_check.py --workdir work [--lang zh]   # errors: missing/extra keys, wrong ｜ count, empty parts
```
It exits 1 on errors (fix and re-run). Review warnings (untranslated lines, length ratios). On success it writes
`work/cues.json` (passes through `"spk"` when present).

### 7. Chapters (agent)
From `lines.tsv` (index + time), write `work/chapters.json`: 8–20 chapters for an hour of audio, each
typically 1–6 min, bilingual titles, first chapter at `i: 0`:
```json
[{"i": 0, "tr": "开场：全员到齐", "en": "Intro: a full house"},
 {"i": 38, "tr": "下周新作速览", "en": "Next week's new releases"}]
```
`i` = index of the first line of the chapter (start times are filled in from the cues). Titles: short and
specific (topic or segment + person), same naming rules as the translation.

### 8. Config + build
Copy `work/config.draft.json` → `config.json` (see below), then:
```bash
python $S/build.py config.json --check      # validates cues/chapters, prints the chapter table with durations
python $S/build.py config.json              # online player (default) -> "output"  (both styles inside)
python $S/build.py config.json --offline    # offline player (DAI feeds / on request) -> <output>_offline.html
                                            # (no suffix when the config itself says "audio_mode": "offline")
python $S/build.py config.json --variant B  # Apple as the DEFAULT style (still one file; no separate _B.html)
```

### 9. Test at phone size
```bash
python $S/test_player.py out/<name>.html --local-audio work/episode.mp3 --shots out/shot   # online build
python $S/test_player.py out/<name>_offline.html --shots out/shot                          # offline: no --local-audio
# or: --chrome /usr/bin/google-chrome   (falls back to google-chrome/chromium if Playwright's browser is missing)
```
Chromium with iPhone (390×844) and Android (412×915) emulation. `--local-audio` serves the mp3 for the
online `audio_url` (HTTP Range, read from disk, each response capped at `--chunk-mb` 2 MB) so the test also
runs offline; full-size files (80+ MB) work (≈ 40 s per run for a 1 h episode). (Older versions answered Chrome's
`bytes=0-` with the whole file in one Playwright fulfill → ~60 s freeze and a browser crash.) Optional
`--test-kbps 24` serves a time-aligned low-bitrate copy (ffmpeg) instead. Tap-to-seek reads the highlight right
after the seek and accepts the tapped line or a neighbour within `--tap-tolerance` (default 1) if currentTime is
in that span. The JSON also reports `audio_duration` (browser vs player) and a warning when they differ by > 3 s
(wrong test audio). It checks: no JS errors, all lines and
chapters rendered, audio loads, tap-to-seek highlights and plays, chapter sheet/jump, prev/next, the
4-state language toggle, remembered strip, **style switch S↔B both ways + persistence**, **speaker labels
shown/hidden toggle** (when cues have `spk`; otherwise graceful no-speaker build). Exit 1 on failure. Look at
the screenshots. If possible, also open the file on a real phone. The online file needs the audio host to allow
cross-site playback (plain `<audio>` playback is fine for normal podcast CDNs).

## File formats (what the agent produces)

**config.json** (relative paths resolve against the config file's directory)
```json
{"show": "Official PlayStation Podcast", "title": "September Selects", "episode": "548",
 "page_url": "https://…", "audio_url": "https://…/Episode_548.mp3", "audio_file": "work/episode.mp3",
 "cover": "work/cover.jpg", "workdir": "work", "cues": "work/cues.json", "chapters": "work/chapters.json",
 "speakers": "work/speakers.json",
 "output": "out/PS_Podcast_548.html", "variant": "S", "audio_mode": "online",
 "source_lang": "en", "target_lang": "zh"}
```
Required: `show`, `title`, `cover` (path or URL), `cues`, `chapters`, plus `audio_url` (online) or
`audio_file`/`audio_url` (offline). Optional: `dynamic_ads` (bool, written by fetch_episode.py; true → build
offline, online builds print a warning); `speakers` (path or inline map); `variant` `S`|`B` (**default
in-page style only** — both styles always ship); `audio_mode` `online`|`offline`; `episode`;
`duration` (else ffprobe of `audio_file`, else the last cue); `theme_color` `"#rrggbb"` (override the cover
colour, e.g. for grey or garish covers); `embed_kbps` (default 40); `storage_key`; title overrides
`short_title`, `page_title`, `top_title`, `top_subtitle`, `episode_title`, `bilingual_tag`. For a non-Chinese
target, set `target_lang` plus `target_label` (e.g. `"ES"`) and `target_name` (`"Español"`). The UI switches
to English labels; `ui` can override any label.

**cues.json** (written by tr_check.py):
`[{"s": 18.14, "e": 21.12, "en": "source line", "tr": "translation", "spk": "S0"}]`
(`"zh"` accepted as alias of `"tr"`; `"spk"` optional — old cues without it still build).
**chapters.json**: `[{"i": 0, "tr": "…", "en": "…"}]`.
**speakers.json**: `{"S0": {"name": "…", "color": "#rrggbb"?}, …}` (or a bare string name).
**diarization.json**: `[{"s": 18.29, "e": 28.1, "spk": 0}, …]` (0-based ints from diarize.py).
**Translation blocks**: `work/tr/bNN.src.txt` (generated) → `work/tr/bNN.<lang>.txt` (agent), `key<TAB>part｜part…`.
**edits.json / fixes.json**: see step 5 and `examples/`.

## Quality checklist
- [ ] Every chunk transcribed (no empty/short chunks); no obviously missing minutes (compare the last cue with the duration).
- [ ] Names, titles and products spelled as on the episode page; hallucinated lines dropped.
- [ ] Lines readable: mostly 6–20 words, rarely more than 26; no line spanning two speakers where it matters.
- [ ] Speakers: `speakers.json` maps every `S*` used in cues to a real name from page.txt / transcript; spot-check
      turn boundaries (a few wrong-speaker lines near overlaps are normal — fix via edits `{"spk":…}`).
- [ ] `tr_check.py` has 0 errors; warnings reviewed; 100 % of lines translated.
- [ ] Official Chinese game and film names used; hosts stay English; consistent terms across blocks (glossary).
- [ ] Chapters: start at 0, cover the whole episode, bilingual, sensible lengths (`build.py --check` table).
- [ ] DAI checked (`dynamic_ads` in meta.json / draft). DAI feed → offline build from the transcribed mp3
      (explain to the user: streamed ads vary by region/time → subtitles drift). Otherwise online (default);
      offline only if requested; online file streams from `audio_url`.
- [ ] Single HTML contains both Spotify and Apple styles; default matches `variant`; style switch + speaker
      show/hide remembered across reload.
- [ ] `test_player.py` passes on both devices (incl. style switch + speakers, or graceful no-speaker); screenshots
      look right (colours from the cover, current line white, speaker pills at turn starts).
- [ ] Deliver the HTML path(s) and sizes. Offline files are ~24 MB/h (HTML) and can be shared as a single file.

## Scripts
| script | purpose |
|---|---|
| `fetch_episode.py` | page / RSS / mp3 → audio, cover, page text, meta, draft config (+ DAI detection) |
| `dai.py` | dynamic-ad-insertion detection (redirect chain, final host, `has-ads`); used by fetch/build; CLI |
| `transcribe.py` | chunked faster-whisper transcription with word timestamps (parallel, resumable) |
| `diarize.py` | sherpa-onnx speaker diarization → `diarization.json` (`--num-speakers` / `--threshold`) |
| `segment.py` | words (+ optional diarization) → subtitle lines; breaks on speaker changes |
| `apply_edits.py` | proofreading edits + regex fixes + per-line/`split` speaker overrides → `lines.json` |
| `tr_split.py` / `tr_check.py` | translation blocks with the ｜ protocol (speaker context) / verification + merge → `cues.json` |
| `build.py` | config → single-file HTML (both styles; `--offline`, `--variant` sets default, `--check`) |
| `theme.py` | cover → theme colours for S and B (used by build.py; CLI preview) |
| `textfix.py` | shared text clean-up |
| `test_player.py` | headless phone-size test (Playwright): style switch, speakers, strip, seek, chapters |

Template: `templates/player.html` (Spotify + Apple in one file). Legacy `player_S.html` / `player_B.html` are
no longer used by `build.py`.
