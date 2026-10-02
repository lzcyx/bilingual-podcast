#!/usr/bin/env python3
"""Build the single-file bilingual subtitle player HTML from a config JSON.

ONE output file per run. Both Spotify (S) and Apple (B) looks live in the same file;
the in-page style switch toggles them without reloading. Config "variant" / --variant
only sets the DEFAULT style (remembered in localStorage after the user switches).

  python build.py config.json                 # ONLINE player (streams "audio_url"), default style from config (S)
  python build.py config.json --offline       # OFFLINE: audio embedded as 40 kbps mono mp3 (~18 MB/hour)
  python build.py config.json --variant B     # Apple-style as the default (still one file with both styles)
  python build.py config.json --check         # validate cues/chapters, print chapter table, no output

Config "audio_mode": "online" (default) | "offline". "output" is the path for the configured mode; when a CLI
flag overrides the config (--offline/--online) and no --out is given, a suffix is added (_offline / _online).
--variant no longer produces a separate _B.html.

Dynamic ad insertion: if config "dynamic_ads" is true (written by fetch_episode.py) — or, when the key is absent,
"audio_url" points at a known DAI host (Megaphone, ART19, Acast, Omny, Simplecast/AdsWizz, …; see dai.py) — an
ONLINE build prints a warning: streamed ads differ per region/time, so subtitles drift. Build --offline instead.

Relative paths in the config are resolved against the config file's directory.
Cues:     [{"s":12.3,"e":15.0,"en":"…","tr":"…", optional "spk":"S0"}, ...]  ("zh" accepted as alias of "tr")
Chapters: [{"i":0,"tr":"开场","en":"Intro"}, ...]  i = index of the first cue of the chapter
Speakers: work/speakers.json or config "speakers": {"S0": {"name":"Sid Shuman","color":"#7eb8ff"}, ...}
"""
import argparse, base64, io, json, os, re, subprocess, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from theme import theme_from  # noqa: E402
import dai  # noqa: E402

TEMPLATES = os.path.join(os.path.dirname(HERE), 'templates')
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36'}

UI_ZH = dict(show='显示', both='双语', srcName='EN', tgtName='中文', tgtFirst='中文在上 / Chinese first',
             strip='底部信息条 / Info strip', autoFollow='自动跟随 / Auto-follow', big='大字号 / Large text',
             on='开', off='关', back='↓ 回到当前 · Back to current', follow='跟随', chapters='章节 · Chapters',
             chCount='{n} 章', chapN='第 {n} 章', min=' 分', sec=' 秒', modeBoth='EN/中', modeSwap='中/EN',
             modeSrc='EN', modeTgt='中文', loadFail='音频加载失败，请检查网络 · Audio failed to load',
             preparing='正在准备音频… {n}% · Preparing audio', decodeFail='音频解码失败 · Audio decode failed',
             style='外观 / Style', styleS='Spotify', styleB='Apple',
             speakers='说话人 / Speakers')


def ui_strings(cfg):
    tl = cfg.get('target_lang', 'zh')
    src_label = cfg.get('source_label', cfg.get('source_lang', 'en').split('-')[0].upper())
    if tl.lower().startswith('zh'):
        ui = dict(UI_ZH)
        ui['srcName'] = ui['modeSrc'] = src_label
        ui['modeBoth'] = f'{src_label}/中'; ui['modeSwap'] = f'中/{src_label}'
        tag = '中英双语' if src_label == 'EN' else '双语'
    else:
        tgt_label = cfg.get('target_label', tl.split('-')[0].upper())
        tgt_name = cfg.get('target_name', tgt_label)
        ui = dict(show='Display', both='Both', srcName=src_label, tgtName=tgt_name, tgtFirst=f'{tgt_name} first',
                  strip='Info strip', autoFollow='Auto-follow', big='Large text', on='On', off='Off',
                  back='↓ Back to current', follow='Follow', chapters='Chapters', chCount='{n} chapters',
                  chapN='Chapter {n}', min=' min', sec=' s', modeBoth=f'{src_label}/{tgt_label}',
                  modeSwap=f'{tgt_label}/{src_label}', modeSrc=src_label, modeTgt=tgt_name,
                  loadFail='Audio failed to load', preparing='Preparing audio… {n}%', decodeFail='Audio decode failed',
                  style='Style', styleS='Spotify', styleB='Apple', speakers='Speakers')
        tag = f'{src_label} · {tgt_label}'
    ui.update(cfg.get('ui', {}))
    return ui, cfg.get('bilingual_tag', tag)


def slug(s):
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')[:60] or 'podcast'


def load_json(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def fetch_bytes(src, base):
    if re.match(r'https?://', src):
        req = urllib.request.Request(src, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    with open(os.path.join(base, src), 'rb') as f:
        return f.read()


def cover_images(raw):
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert('RGB')
    art = im.copy()
    art.thumbnail((720, 720))
    w, h = im.size; m = min(w, h)
    icon = im.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m)).resize((180, 180), Image.LANCZOS)
    enc = lambda x, q: (lambda b: (x.save(b, 'JPEG', quality=q, optimize=True), b.getvalue())[1])(io.BytesIO())
    return im, enc(art, 80), enc(icon, 85)


def ffprobe_duration(path):
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                             capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except Exception:
        return None


def embedded_audio(src_path, cache_dir, kbps=40):
    """Transcode to mono mp3 for embedding; cached in cache_dir, re-made when the source file changes."""
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, f'audio_mono{kbps}.mp3')
    st = os.stat(src_path)
    stamp = dict(src=os.path.abspath(src_path), size=st.st_size, mtime=int(st.st_mtime), kbps=kbps)
    side = out + '.src.json'
    old = None
    if os.path.exists(side):
        try:
            old = load_json(side)
        except Exception:
            old = None
    fresh = os.path.exists(out) and os.path.getmtime(out) >= st.st_mtime and (old is None or old == stamp)
    if not fresh:
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', src_path, '-vn', '-map_metadata', '-1',
                        '-ac', '1', '-ar', '22050', '-b:a', f'{kbps}k', out + '.tmp.mp3'], check=True)
        os.replace(out + '.tmp.mp3', out)
    with open(side, 'w', encoding='utf-8') as f:
        json.dump(stamp, f)
    return out


def dai_warning(cfg, mode):
    """Warn when streaming a dynamic-ad-insertion feed (ads differ per listener -> subtitle drift)."""
    if mode != 'online':
        return
    flag = cfg.get('dynamic_ads')
    why = ''
    if flag is None and cfg.get('audio_url'):
        r = dai.detect(cfg['audio_url'], network=False)
        if r['dynamic_ads']:
            flag, why = True, ' (audio_url host: ' + ', '.join(r['providers']) + ')'
    if flag:
        print('!' * 78 + '\nWARNING: this feed uses DYNAMIC AD INSERTION' + why + ' but the build is ONLINE.\n'
              '  The stream stitches different pre/mid-roll ads per region / time / device, so other listeners\n'
              '  (e.g. in China) hear a differently-timed file and the subtitles drift after every ad break.\n'
              '  Recommended: python build.py <config> --offline   (or "audio_mode": "offline" in the config)\n'
              + '!' * 78, file=sys.stderr)


def norm_cues(cues):
    out, errs = [], []
    for k, c in enumerate(cues):
        tr = c.get('tr', c.get('zh'))
        if tr is None or not str(tr).strip():
            errs.append(f'cue {k}: missing translation')
        if not str(c.get('en', '')).strip():
            errs.append(f'cue {k}: empty source text')
        row = {'s': round(float(c['s']), 2), 'e': round(float(c['e']), 2),
               'en': str(c.get('en', '')).strip(), 'tr': str(tr or '').strip()}
        spk = c.get('spk')
        if spk:
            row['spk'] = str(spk)
        out.append(row)
    for k in range(1, len(out)):
        if out[k]['s'] < out[k - 1]['s']:
            errs.append(f'cue {k}: start {out[k]["s"]} earlier than previous cue')
        if out[k - 1]['e'] > out[k]['s']:
            out[k - 1]['e'] = out[k]['s']
    return out, errs


def norm_chapters(ch, cues, duration):
    out, errs = [], []
    for k, c in enumerate(ch):
        i = int(c['i'])
        if not 0 <= i < len(cues):
            errs.append(f'chapter {k}: cue index {i} out of range'); continue
        tr = c.get('tr', c.get('zh'))
        if not tr or not c.get('en'):
            errs.append(f'chapter {k}: needs both "tr" and "en" titles')
        s = 0.0 if k == 0 else float(c.get('s', cues[i]['s']))
        if k and abs(s - cues[i]['s']) > 0.5:
            s = cues[i]['s']
        out.append({'i': i, 's': round(s, 2), 'tr': tr or '', 'en': c.get('en', '')})
    if out and out[0]['i'] != 0:
        errs.append('first chapter must start at cue 0 (i=0)')
    for a, b in zip(out, out[1:]):
        if b['i'] <= a['i']:
            errs.append(f'chapters not strictly increasing at cue {b["i"]}')
    table = []
    for k, c in enumerate(out):
        e = out[k + 1]['s'] if k + 1 < len(out) else duration
        table.append(f"{k + 1:>3} cue {c['i']:>4}  {int(c['s'] // 60):>3}:{c['s'] % 60:04.1f}  {(e - c['s']) / 60:5.1f}m  {c['tr']} / {c['en']}")
    return out, errs, table


def load_speakers(cfg, base, workdir, cues):
    """Return {S0: {name, color?}, ...}. Missing file / empty → {} (player degrades gracefully)."""
    candidates = []
    if cfg.get('speakers'):
        if isinstance(cfg['speakers'], str):
            candidates.append(cfg['speakers'] if os.path.isabs(cfg['speakers']) or re.match(r'https?://', cfg['speakers'])
                              else os.path.join(base, cfg['speakers']))
        elif isinstance(cfg['speakers'], dict):
            return _fill_speakers(_norm_speakers(cfg['speakers']), cues)
    candidates += [os.path.join(workdir, 'speakers.json'), os.path.join(base, 'speakers.json')]
    raw = {}
    for p in candidates:
        if p and os.path.exists(p):
            raw = load_json(p)
            break
    # Also invent placeholder entries for any spk ids present in cues but missing from map
    return _fill_speakers(_norm_speakers(raw), cues)


def _fill_speakers(names, cues):
    """Keep only ids used by the cues; unnamed ids fall back to the raw id (e.g. "S3")."""
    spk_ids = sorted({c['spk'] for c in cues if c.get('spk')}, key=lambda x: (len(x), x))
    return {sid: names.get(sid, {'name': sid}) for sid in spk_ids}


def _norm_speakers(raw):
    out = {}
    for k, v in (raw or {}).items():
        key = str(k) if str(k).startswith('S') else f'S{k}'
        if isinstance(v, str):
            out[key] = {'name': v}
        elif isinstance(v, dict):
            name = v.get('name') or key
            row = {'name': name}
            if v.get('color'):
                out_c = str(v['color'])
                if not out_c.startswith('#'):
                    out_c = '#' + out_c
                row['color'] = out_c
            out[key] = row
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('config')
    ap.add_argument('--variant', choices=['S', 'B'], help='default in-page style (both styles ship in one file)')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--offline', action='store_true', help='embed the audio (overrides config audio_mode)')
    g.add_argument('--online', action='store_true', help='stream audio_url (overrides config audio_mode)')
    ap.add_argument('--out')
    ap.add_argument('--check', action='store_true', help='validate only')
    a = ap.parse_args()

    cfg = load_json(a.config)
    base = os.path.dirname(os.path.abspath(a.config))
    P = lambda p: p if (p is None or re.match(r'https?://', p) or os.path.isabs(p)) else os.path.join(base, p)
    workdir = P(cfg.get('workdir', '.'))
    variant = a.variant or cfg.get('variant', 'S')
    if variant not in ('S', 'B'):
        sys.exit('variant must be "S" or "B"')
    ALIAS = {'online': 'online', 'lite': 'online', 'url': 'online', 'offline': 'offline', 'embedded': 'offline', 'embed': 'offline'}
    cfg_mode = ALIAS.get(str(cfg.get('audio_mode', 'online')).lower())
    if not cfg_mode:
        sys.exit('audio_mode must be "online" or "offline"')
    mode = 'offline' if a.offline else 'online' if a.online else cfg_mode
    dai_warning(cfg, mode)

    cues, errs = norm_cues(load_json(P(cfg.get('cues', 'cues.json'))))
    audio_file = P(cfg['audio_file']) if cfg.get('audio_file') else None
    duration = float(cfg.get('duration') or (audio_file and os.path.exists(audio_file) and ffprobe_duration(audio_file)) or cues[-1]['e'] + 1)
    chapters, cerrs, table = norm_chapters(load_json(P(cfg.get('chapters', 'chapters.json'))), cues, duration)
    errs += cerrs
    speakers = load_speakers(cfg, base, workdir, cues)
    nspk = len({c.get('spk') for c in cues if c.get('spk')})
    print(f'{len(cues)} cues, {len(chapters)} chapters, duration {duration:.1f}s'
          + (f', {nspk} speakers' if nspk else ', no speaker data'))
    print('\n'.join(table))
    if errs:
        print('ERRORS:\n  ' + '\n  '.join(errs[:50])); sys.exit(1)
    if a.check:
        return

    show = cfg['show']; title = cfg['title']; ep = str(cfg.get('episode', '')).strip()
    ui, tag = ui_strings(cfg)
    ep_label = f'Ep. {ep}' if ep else ''
    L = dict(
        short=cfg.get('short_title') or (f'{show} {ep}'.strip() if len(show) <= 18 else (f'Ep. {ep}' if ep else title))[:30],
        pageTitle=cfg.get('page_title') or ' · '.join(x for x in [f'{show} {ep}'.strip(), title, tag] if x),
        topTitle=cfg.get('top_title') or title,
        topSub=cfg.get('top_subtitle') or ' · '.join(x for x in [show, ep_label, tag] if x),
        epTitle=cfg.get('episode_title') or (f'{ep_label}: {title}' if ep else title))

    raw_cover = fetch_bytes(cfg['cover'], base)
    img, art, icon = cover_images(raw_cover)
    th = theme_from(img, cfg.get('theme_color'))

    key = cfg.get('storage_key') or slug(f'{show}_{ep or title}')
    player_cfg = dict(key=key, short=L['short'], pageTitle=L['pageTitle'], topTitle=L['topTitle'], topSub=L['topSub'],
                      epTitle=L['epTitle'], show=show, duration=round(duration, 2), ui=ui,
                      defaultTheme=variant)
    audio_b64 = ''
    if mode == 'online':
        if not cfg.get('audio_url'):
            sys.exit('online mode needs "audio_url" (a directly playable mp3 URL)')
        player_cfg.update(audio='url', audioUrl=cfg['audio_url'])
    else:
        src = audio_file
        if not src or not os.path.exists(src):
            if not cfg.get('audio_url'):
                sys.exit('offline mode needs "audio_file" or "audio_url"')
            src = os.path.join(workdir, 'episode_download.mp3')
            if cfg.get('dynamic_ads'):
                print('WARNING: dynamic_ads feed and no audio_file: a fresh download carries a DIFFERENT ad set than '
                      'the one that was transcribed -> subtitles will be offset. Point "audio_file" at the '
                      'transcribed mp3.', file=sys.stderr)
            if not os.path.exists(src):
                print('downloading audio …'); open(src, 'wb').write(fetch_bytes(cfg['audio_url'], base))
        mp3 = embedded_audio(src, workdir, int(cfg.get('embed_kbps', 40)))
        emb_dur = ffprobe_duration(mp3)
        if emb_dur and cues and cues[-1]['s'] > emb_dur + 1:
            print(f'WARNING: last cue starts at {cues[-1]["s"]:.1f}s but the embedded audio is only {emb_dur:.1f}s '
                  '— wrong audio_file?', file=sys.stderr)
        elif emb_dur and abs(emb_dur - duration) > 3:
            print(f'WARNING: embedded audio is {emb_dur:.1f}s but duration is {duration:.1f}s', file=sys.stderr)
        audio_b64 = base64.b64encode(open(mp3, 'rb').read()).decode()
        player_cfg.update(audio='embed')

    t = open(os.path.join(TEMPLATES, 'player.html'), encoding='utf-8').read()
    J = lambda o: json.dumps(o, ensure_ascii=False).replace('</', '<\\/')
    E = lambda s: str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    rep = {'__ICON__': base64.b64encode(icon).decode(), '__ART__': base64.b64encode(art).decode(),
           '__HTML_LANG__': 'zh-CN' if cfg.get('target_lang', 'zh').lower().startswith('zh') else cfg['target_lang'],
           '__SHORT__': E(L['short']), '__PAGETITLE__': E(L['pageTitle']), '__TOPTITLE__': E(L['topTitle']),
           '__TOPSUB__': E(L['topSub']), '__EPTITLE__': E(L['epTitle']), '__SHOWNAME__': E(show),
           '__CONFIG__': J(player_cfg), '__CUES__': J(cues), '__CHAPTERS__': J(chapters),
           '__SPEAKERS__': J(speakers), '__DEFAULT_THEME__': variant,
           '__C_BASE_S__': th['S']['__C_BASE__'], '__C_TOP_S__': th['S']['__C_TOP__'],
           '__C_BASE_B__': th['B']['__C_BASE__'], '__C_BAR_B__': th['B']['__C_BAR__'],
           '__C_SHEET_B__': th['B']['__C_SHEET__']}
    rep.update({f'__UI_{k}__': E(v) for k, v in ui.items()})
    for k, v in rep.items():
        t = t.replace(k, v)
    left = sorted(set(re.findall(r'__[A-Z][A-Za-z0-9_]+__', t)) - {'__AUDIO__'})
    if left:
        sys.exit(f'unfilled placeholders: {left}')
    t = t.replace('__AUDIO__', audio_b64)

    out = P(a.out) if a.out else P(cfg.get('output') or f'out/{slug(show)}_{ep or slug(title)}_player.html')
    if not a.out:
        root, ext = os.path.splitext(out)
        if mode != cfg_mode:
            root = root + ('_offline' if mode == 'offline' else '_online')
        # --variant no longer suffixes a separate file; default style is inside the one HTML
        out = root + ext
    os.makedirs(os.path.dirname(os.path.abspath(out)) or '.', exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(t)
    print(f'wrote {out}  {os.path.getsize(out) / 1e6:.2f} MB  default_style={variant} audio={mode} '
          f'theme={th["dominant"]} speakers={len(speakers)}')


if __name__ == '__main__':
    main()
