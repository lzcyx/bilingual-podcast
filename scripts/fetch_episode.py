#!/usr/bin/env python3
"""Collect episode audio + metadata + cover + page text, and write a draft config.

  python fetch_episode.py --page https://…/episode-page  [--rss https://…/feed.xml] --workdir work
  python fetch_episode.py --mp3 https://…/episode.mp3 --rss https://…/feed.xml --workdir work
  add --no-audio to skip downloading the mp3 (e.g. when you already have it)
  --audio-mode auto|online|offline   draft "audio_mode" (auto: offline when dynamic ad insertion is detected)
  --no-dai-probe                     detect DAI from the URL text only (no redirect probe)

Writes: work/meta.json (everything found), work/page.txt (readable page text: use it for proper nouns, the
glossary and the transcription prompt), work/episode.mp3, work/cover.<ext>, work/config.draft.json.
Heuristics only: always review meta.json / config.draft.json; fill anything missing by hand.

Dynamic ad insertion (DAI): the audio URL is probed (redirects followed, final host + `has-ads` header checked)
for Megaphone, ART19, Simplecast/AdsWizz, Acast, Omny, Spreaker, Audioboom, … (also behind podtrac / pscrb.fm /
mgln.ai / chtbl prefix chains). DAI hosts stitch different ads per region/time, so the transcript only matches the
file WE downloaded: meta.json + config.draft.json get "dynamic_ads": true and the draft defaults to
"audio_mode": "offline" (embed that file). See dai.py.
"""
import argparse, html, json, os, re, sys, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dai  # noqa: E402

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36'}
ITUNES = '{http://www.itunes.com/dtds/podcast-1.0.dtd}'


def get(url, binary=False, timeout=120):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        data = r.read()
        return data if binary else data.decode(r.headers.get_content_charset() or 'utf-8', 'replace')


def download(url, path):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=600) as r, open(path + '.part', 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0); n = 0
        while True:
            b = r.read(1 << 20)
            if not b: break
            f.write(b); n += len(b)
            if total: print(f'\r  {n / 1e6:.1f}/{total / 1e6:.1f} MB', end='', flush=True)
    os.replace(path + '.part', path); print()


class Text(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'header', 'form'}
    BLOCK = {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'br', 'tr', 'section', 'article'}

    def __init__(self):
        super().__init__(); self.out = []; self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP: self.skip += 1
        if tag in self.BLOCK: self.out.append('\n')
        if tag == 'li': self.out.append('• ')

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip: self.skip -= 1
        if tag in self.BLOCK: self.out.append('\n')

    def handle_data(self, d):
        if not self.skip: self.out.append(d)

    def text(self):
        t = re.sub(r'[ \t\r\f\v]+', ' ', ''.join(self.out))
        return re.sub(r'\n\s*\n+', '\n', t).strip()


def meta_tag(src, *names):
    for n in names:
        for pat in (rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]*content=["\']([^"\']+)',
                    rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{re.escape(n)}["\']'):
            m = re.search(pat, src, re.I)
            if m: return html.unescape(m.group(1)).strip()
    return None


def from_page(url):
    src = get(url)
    body = src
    m = re.search(r'<article\b.*?</article>', src, re.S | re.I)
    if m and len(m.group(0)) > 2000: body = m.group(0)
    tp = Text(); tp.feed(body)
    mp3s = []
    for u in re.findall(r'https?://[^\s"\'<>]+?\.(?:mp3|m4a)(?:\?[^\s"\'<>]*)?', src):
        u = html.unescape(u)
        if u not in mp3s: mp3s.append(u)
    title = meta_tag(src, 'og:title', 'twitter:title')
    if not title:
        m = re.search(r'<title>(.*?)</title>', src, re.S | re.I); title = html.unescape(m.group(1)).strip() if m else None
    return src, tp.text(), dict(page_url=url, page_title=title, image=meta_tag(src, 'og:image', 'twitter:image'),
                                description=meta_tag(src, 'og:description', 'description'),
                                site=meta_tag(src, 'og:site_name'), audio_candidates=mp3s)


def from_rss(feed, page=None, mp3=None, title_hint=None):
    root = ET.fromstring(get(feed).encode('utf-8'))
    ch = root.find('channel')
    show = (ch.findtext('title') or '').strip()
    img = ch.find(ITUNES + 'image')
    show_img = img.get('href') if img is not None else ch.findtext('image/url')

    def norm(u): return urllib.parse.urlsplit(u or '')._replace(query='', fragment='').geturl().rstrip('/')
    best = None
    for it in ch.findall('item'):
        enc = it.find('enclosure'); eu = enc.get('url') if enc is not None else None
        link = it.findtext('link') or ''; t = (it.findtext('title') or '').strip()
        score = 0
        if mp3 and eu and os.path.basename(norm(mp3)) == os.path.basename(norm(eu)): score = 3
        elif page and norm(link) == norm(page): score = 3
        elif title_hint and t and (t.lower() in title_hint.lower() or title_hint.lower() in t.lower()): score = 2
        if score and (not best or score > best[0]):
            ii = it.find(ITUNES + 'image')
            best = (score, dict(title=t, audio_url=eu, link=link, pub_date=it.findtext('pubDate'),
                                episode=it.findtext(ITUNES + 'episode'), duration=it.findtext(ITUNES + 'duration'),
                                image=ii.get('href') if ii is not None else None,
                                description=re.sub(r'[ \t]+', ' ', html.unescape(re.sub('<[^>]+>', ' ', it.findtext('description') or ''))).strip()))
    return dict(show=show, show_image=show_img, item=best[1] if best else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--page'); ap.add_argument('--mp3'); ap.add_argument('--rss')
    ap.add_argument('--workdir', default='work')
    ap.add_argument('--no-audio', action='store_true')
    ap.add_argument('--audio-mode', choices=['auto', 'online', 'offline'], default='auto',
                    help='draft audio_mode (auto = offline when dynamic ad insertion is detected, else online)')
    ap.add_argument('--no-dai-probe', action='store_true', help='DAI check from URL text only (no network probe)')
    a = ap.parse_args()
    if not (a.page or a.mp3): sys.exit('give --page and/or --mp3')
    wd = a.workdir; os.makedirs(wd, exist_ok=True)
    meta = {'page_url': a.page, 'audio_url': a.mp3}
    if a.page:
        src, text, pm = from_page(a.page)
        open(os.path.join(wd, 'page.html'), 'w', encoding='utf-8').write(src)
        open(os.path.join(wd, 'page.txt'), 'w', encoding='utf-8').write(f'URL: {a.page}\nTITLE: {pm["page_title"]}\n\n{text}\n')
        meta.update({k: v for k, v in pm.items() if v})
        if not meta.get('audio_url') and pm['audio_candidates']: meta['audio_url'] = pm['audio_candidates'][0]
    if a.rss:
        r = from_rss(a.rss, a.page, meta.get('audio_url'), meta.get('page_title'))
        meta['show'] = r['show']; meta['rss'] = r
        it = r['item'] or {}
        meta['audio_url'] = meta.get('audio_url') or it.get('audio_url')
        meta.setdefault('image', it.get('image') or r['show_image'])
        if not meta.get('image'): meta['image'] = it.get('image') or r['show_image']
        meta['episode'] = it.get('episode'); meta['rss_title'] = it.get('title')
        if not a.page and it.get('description'):
            open(os.path.join(wd, 'page.txt'), 'w', encoding='utf-8').write(f"TITLE: {it.get('title')}\n\n{it['description']}\n")
    ep = meta.get('episode') or (re.search(r'(?:episode|ep\.?)\s*#?\s*(\d+)', meta.get('page_title') or meta.get('rss_title') or '', re.I) or [None, None])[1]
    title = meta.get('rss_title') or meta.get('page_title') or ''
    title = re.sub(r'\s+[–|—-]\s+[^–|—-]+$', '', title)          # drop " – Site Name"
    m = re.search(r'(?:episode|ep\.?)\s*#?\s*(\d+)\s*[:\-–|]\s*(.+)$', title, re.I)
    show_name = meta.get('show') or meta.get('site') or ''
    if not m and show_name and title.lower().startswith(show_name.lower()):
        # "IGN UK Podcast 867: The Big Silent Hill Chat" -> ep 867, "The Big Silent Hill Chat"
        m = re.match(r'\s*[:\-–|]?\s*#?(\d{1,5})\s*[:\-–|]\s*(.+)$', title[len(show_name):])
    if not m:
        m = re.match(r'\s*#?(\d{1,5})\s*[:\-–|]\s*(.+)$', title)                # "867: Title"
    short_title = m.group(2).strip() if m else title
    ep = ep or (m.group(1) if m else None)
    dai_info = None
    if meta.get('audio_url'):
        item = (meta.get('rss') or {}).get('item') or {}
        notes = ' '.join(x for x in [item.get('description'), meta.get('description')] if x)
        dai_info = dai.detect(meta['audio_url'], text=notes, network=not a.no_dai_probe)
        meta['dynamic_ads'] = dai_info['dynamic_ads']; meta['dai'] = dai_info
        print('audio chain:', ' -> '.join(dai_info['chain']) + (f'  (probe: {dai_info["probe_error"]})' if dai_info['probe_error'] else ''))
    dyn = bool(dai_info and dai_info['dynamic_ads'])
    audio_mode = a.audio_mode if a.audio_mode != 'auto' else ('offline' if dyn else 'online')
    cover = None
    if meta.get('image'):
        ext = os.path.splitext(urllib.parse.urlsplit(meta['image']).path)[1].lower() or '.jpg'
        cover = os.path.join(wd, 'cover' + (ext if ext in ('.jpg', '.jpeg', '.png', '.webp') else '.jpg'))
        try: open(cover, 'wb').write(get(meta['image'], binary=True))
        except Exception as e: print('cover download failed:', e); cover = None
    if meta.get('audio_url') and not a.no_audio:
        print('downloading audio', meta['audio_url']); download(meta['audio_url'], os.path.join(wd, 'episode.mp3'))
    json.dump(meta, open(os.path.join(wd, 'meta.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    rel = lambda p: os.path.abspath(p) if p else None  # absolute: config paths resolve relative to the config file
    show = meta.get('show') or meta.get('site') or 'TODO show name'
    draft = {'show': show, 'title': short_title or 'TODO episode title',
             'episode': ep or '', 'page_url': a.page or '', 'audio_url': meta.get('audio_url') or 'TODO direct mp3 URL',
             'audio_file': rel(os.path.join(wd, 'episode.mp3')), 'cover': rel(cover) or meta.get('image') or 'TODO cover',
             'workdir': rel(wd), 'cues': rel(os.path.join(wd, 'cues.json')), 'chapters': rel(os.path.join(wd, 'chapters.json')),
             'output': os.path.abspath(f'out/{re.sub(r"[^A-Za-z0-9]+", "_", show).strip("_")[:40]}_{ep or "episode"}.html'),
             'variant': 'S', 'audio_mode': audio_mode, 'dynamic_ads': dyn, 'source_lang': 'en', 'target_lang': 'zh'}
    json.dump(draft, open(os.path.join(wd, 'config.draft.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(draft, ensure_ascii=False, indent=1))
    print(f'audio candidates: {meta.get("audio_candidates", [])[:5]}')
    if dyn:
        print('\n' + '!' * 78 + f'\nWARNING: dynamic ad insertion detected ({", ".join(dai_info["providers"]) or "DAI"}): '
              + '; '.join(dai_info['reasons']) + '\n  Ads are stitched per request (region / time / device). Our download'
              ' (work/episode.mp3) contains ONE ad set; listeners streaming audio_url get different pre/mid-rolls, so'
              ' subtitles would drift.\n  -> config.draft.json: "dynamic_ads": true, "audio_mode": "' + audio_mode + '"'
              + (' (offline = embed the transcribed file; recommended)' if audio_mode == 'offline' else
                 '  (online forced by --audio-mode: subtitles WILL drift for other listeners)') + '\n' + '!' * 78)


if __name__ == '__main__':
    main()
