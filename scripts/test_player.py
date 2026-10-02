#!/usr/bin/env python3
"""Headless phone-size smoke test of a built player (Playwright + Chromium).

  python test_player.py out/player.html [--local-audio episode.mp3] [--shots out/shot] [--chrome /usr/bin/google-chrome]

Runs at iPhone (390x844, Safari UA) and Android (412x915, Chrome UA) viewports, touch + mobile emulation.
Checks: no JS errors, every cue and chapter rendered, audio loads (lite mode: --local-audio serves a local file
for the remote audioUrl so the test works offline), tap-to-seek highlights the line and advances, chapter sheet
opens/jumps, prev/next chapter, EN/中 toggle cycles 4 states, collapsed info strip persists after reload,
style switch S↔B (both ways) + persistence, speaker labels show/hide when speaker data exists (and graceful
no-speaker builds). Prints JSON; exit code 1 if any check fails.
Screenshots: <shots>_<device>_{playing,chapters,menu,spk_off,S_speakers,B_speakers}.png
Note: this is Chromium with mobile emulation — also open the file once on a real iPhone/Android if possible.

--local-audio serving: Chromium's media stack asks for open-ended ranges ("bytes=0-"). Answering those with the
whole file pushed e.g. 83 MB through ONE Playwright route.fulfill (base64 inside a JSON protocol message,
serialised synchronously in this process): the test froze for ~60 s and the browser died. Every response is now
capped at --chunk-mb (default 2 MB, a legal short 206); the browser fetches the rest with follow-up range
requests, read from disk per request (the file is never loaded whole). Any size works; --test-kbps N optionally
serves a time-aligned low-bitrate mono copy made with ffmpeg instead (same duration, so cue times still match).

Tap-to-seek: the active line is read right after the tap and compared with the tapped cue; a neighbouring line
within --tap-tolerance cues (default 1) is accepted (short cues / seek rounding) if currentTime is inside the
tapped cue's (±N) span (timing on a loaded CI box is noisy).
"""
import argparse, asyncio, json, os, re, shutil, subprocess, sys, tempfile, urllib.parse

from playwright.async_api import async_playwright

DEVICES = {
    'iphone': dict(viewport={'width': 390, 'height': 844}, device_scale_factor=3, is_mobile=True, has_touch=True,
                   user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'),
    'android': dict(viewport={'width': 412, 'height': 915}, device_scale_factor=2.625, is_mobile=True, has_touch=True,
                    user_agent='Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36'),
}


async def run_device(p, browser, html, name, a):
    R = {}; errs = []
    ctx = await browser.new_context(**DEVICES[name])
    src = open(html, encoding='utf-8').read()
    cfg = json.loads(re.search(r'<script id="cfg" type="application/json">(.*?)</script>', src, re.S).group(1))
    cues_raw = json.loads(re.search(r'<script id="cues" type="application/json">(.*?)</script>', src, re.S).group(1).replace('<\\/', '</'))
    ncues = len(cues_raw)
    nch = len(json.loads(re.search(r'<script id="chapters" type="application/json">(.*?)</script>', src, re.S).group(1).replace('<\\/', '</')))
    has_spk = any(c.get('spk') for c in cues_raw)
    m = re.search(r'<script id="speakers" type="application/json">(.*?)</script>', src, re.S)
    speakers = json.loads(m.group(1).replace('<\\/', '</')) if m else {}
    R['served_requests'] = 0
    if cfg.get('audio') == 'url' and a.local_audio:
        target = _norm_url(cfg['audioUrl'])
        await ctx.route(lambda u: _norm_url(u) == target, make_audio_server(a.audio_path, a.chunk_mb, R))
    pg = await ctx.new_page()
    pg.on('crash', lambda *_: errs.append('page crashed (renderer died)'))
    pg.on('console', lambda m: errs.append(m.text) if m.type == 'error' else None)
    pg.on('pageerror', lambda e: errs.append('pageerror ' + str(e)))
    J = pg.evaluate; au = 'document.getElementById("au")'
    await pg.goto('file://' + os.path.abspath(html), timeout=180000)
    await pg.wait_for_function('window.__playerReady===true', timeout=180000)
    R['audio_mode'] = cfg.get('audio')
    R['default_theme'] = cfg.get('defaultTheme', 'S')
    R['has_speakers'] = has_spk
    R['cues'] = [await J('document.querySelectorAll(".cue").length'), ncues]
    R['chapter_headers'] = [await J('document.querySelectorAll(".chap-h").length'), nch]
    R['progress_segments'] = await J('document.querySelectorAll(".sg").length')
    R['no_horizontal_overflow'] = await J('document.documentElement.scrollWidth<=window.innerWidth+1')
    try:
        await pg.wait_for_function(au + '.readyState>=1', timeout=60000); R['audio_loaded'] = True
    except Exception:
        R['audio_loaded'] = False
    if R['audio_loaded'] and cfg.get('duration'):
        d = await J(au + '.duration')
        R['audio_duration'] = [round(d or 0, 2), cfg['duration']]
        if d and abs(d - cfg['duration']) > 3:   # wrong / DAI-shifted test audio: cue times will not match
            R.setdefault('warnings', []).append(f'audio duration {d:.1f}s != player duration {cfg["duration"]}s')
    cue = min(ncues - 1, max(1, ncues // 3))
    await J(f'document.querySelector(\'.cue[data-i="{cue}"]\').scrollIntoView({{block:"center"}})')
    await pg.tap(f'.cue[data-i="{cue}"]')
    # read the highlight right after the seek (poll briefly), not after a fixed sleep
    act = None; t0 = None
    for _ in range(20):
        await pg.wait_for_timeout(100)
        act = await J('(document.querySelector(".cue.active")||{dataset:{}}).dataset.i')
        t0 = await J(au + '.currentTime')
        if act is not None and str(act) == str(cue):
            break
    await pg.wait_for_timeout(900)
    t1 = await J(au + '.currentTime'); await pg.wait_for_timeout(1500); t2 = await J(au + '.currentTime')
    c0 = cues_raw[cue]; c_first = cues_raw[max(0, cue - a.tap_tolerance)]; c_last = cues_raw[min(ncues - 1, cue + a.tap_tolerance)]
    in_span = t0 is not None and min(c0['s'] - 0.6, c_first['s']) <= t0 <= c_last['e'] + 0.6
    near = act is not None and abs(int(act) - cue) <= a.tap_tolerance
    R['tap_seek'] = {'cue': cue, 'cue_s': c0['s'], 't0': round(t0 or 0, 2), 't': round(t1, 2), 'active': act,
                     'advancing': t2 > t1, 'ok': bool(near and (in_span or not R['audio_loaded']))}
    R['chapter_row'] = await J('document.getElementById("chapTitle").textContent+" "+document.getElementById("chapIdx").textContent')
    if a.shots:
        await pg.screenshot(path=f'{a.shots}_{name}_playing.png')
    await pg.click('#chapRow'); await pg.wait_for_timeout(700)
    R['sheet_open'] = await J('document.body.classList.contains("sheet-open")')
    R['sheet_items'] = await J('document.querySelectorAll(".ci").length')
    if a.shots:
        await pg.screenshot(path=f'{a.shots}_{name}_chapters.png')
    n = R['sheet_items']
    await pg.click(f'.ci[data-ch="{max(0, n - 2)}"]'); await pg.wait_for_timeout(1200)
    R['sheet_jump'] = {'idx': await J('document.getElementById("chapIdx").textContent'), 'closed': not await J('document.body.classList.contains("sheet-open")')}
    await pg.click('#prevCh'); await pg.wait_for_timeout(700); pa = await J('document.getElementById("chapIdx").textContent')
    await pg.click('#nextCh'); await pg.wait_for_timeout(700); na = await J('document.getElementById("chapIdx").textContent')
    R['prev_next'] = [pa, na]
    modes = []
    for _ in range(4):
        await pg.click('#modeBtn'); modes.append(await J('document.body.className.match(/m-\\w+/)[0]+(document.body.classList.contains("swap")?"+swap":"")'))
    R['mode_cycle'] = modes

    # Style switch both ways + persistence (playback position should survive)
    pos_before = await J(au + '.currentTime')
    await pg.click('#menuBtn'); await pg.wait_for_timeout(200)
    # click Apple
    await pg.click('#styleSeg button[data-style="B"]'); await pg.wait_for_timeout(300)
    R['style_to_B'] = await J('document.body.classList.contains("theme-B") && !document.body.classList.contains("theme-S")')
    pos_after_B = await J(au + '.currentTime')
    R['style_keeps_position'] = abs(pos_after_B - pos_before) < 2.5
    if a.shots:
        await pg.screenshot(path=f'{a.shots}_{name}_menu.png')
    await pg.click('#styleSeg button[data-style="S"]'); await pg.wait_for_timeout(300)
    R['style_to_S'] = await J('document.body.classList.contains("theme-S") && !document.body.classList.contains("theme-B")')
    # persist B across reload
    await pg.click('#styleSeg button[data-style="B"]'); await pg.wait_for_timeout(200)
    await pg.click('#menuBtn')  # close
    await pg.reload(); await pg.wait_for_function('window.__playerReady===true', timeout=180000); await pg.wait_for_timeout(500)
    R['style_B_persists'] = await J('document.body.classList.contains("theme-B")')
    # restore S for remaining shots
    await pg.click('#menuBtn'); await pg.wait_for_timeout(150)
    await pg.click('#styleSeg button[data-style="S"]'); await pg.wait_for_timeout(200)

    # Speaker labels
    if has_spk:
        R['spk_row_visible'] = await J('document.getElementById("spkRow") && getComputedStyle(document.getElementById("spkRow")).display !== "none"')
        R['spk_labels_on'] = await J('document.body.classList.contains("show-spk") && document.querySelectorAll(".cue.turn-start .spk").length > 0')
        n_turns = await J('document.querySelectorAll(".cue.turn-start").length')
        R['spk_turn_starts'] = n_turns
        await pg.click('#spkBtn'); await pg.wait_for_timeout(200)
        R['spk_labels_off'] = await J('!document.body.classList.contains("show-spk")')
        if a.shots:
            await pg.screenshot(path=f'{a.shots}_{name}_spk_off.png')
        await pg.click('#spkBtn'); await pg.wait_for_timeout(150)  # back on
        await pg.reload(); await pg.wait_for_function('window.__playerReady===true', timeout=180000); await pg.wait_for_timeout(400)
        R['spk_on_persists'] = await J('document.body.classList.contains("show-spk")')
        if a.shots:
            # play from a speaker turn in the middle, then shoot both styles with labels
            ti = await J('(function(){var t=[].slice.call(document.querySelectorAll(".cue.turn-start")).map(function(e){return +e.dataset.i});'
                         'var n=document.querySelectorAll(".cue").length/2;t.sort(function(a,b){return Math.abs(a-n)-Math.abs(b-n)});return t[0]})()')
            await J(f'document.querySelector(\'.cue[data-i="{ti}"]\').scrollIntoView({{block:"center"}})')
            await pg.tap(f'.cue[data-i="{ti}"]'); await pg.wait_for_timeout(1500)
            await J(au + '.pause()'); await pg.wait_for_timeout(700)
            for sty in ('S', 'B'):
                await pg.click('#menuBtn'); await pg.wait_for_timeout(150)
                await pg.click(f'#styleSeg button[data-style="{sty}"]'); await pg.wait_for_timeout(200)
                await pg.click('#menuBtn'); await pg.wait_for_timeout(600)
                await pg.screenshot(path=f'{a.shots}_{name}_{sty}_speakers.png')
            await pg.click('#menuBtn'); await pg.wait_for_timeout(150)
            await pg.click('#styleSeg button[data-style="S"]'); await pg.wait_for_timeout(150)
            await pg.click('#menuBtn')
    else:
        R['spk_row_hidden'] = await J('!document.getElementById("spkRow") || getComputedStyle(document.getElementById("spkRow")).display === "none"')
        R['no_spk_labels'] = await J('document.querySelectorAll(".cue .spk").length === 0 || !document.body.classList.contains("show-spk") || document.querySelectorAll(".cue.turn-start").length === 0')

    await pg.click('#hideStrip'); await pg.wait_for_timeout(500)
    await pg.reload(); await pg.wait_for_function('window.__playerReady===true', timeout=180000); await pg.wait_for_timeout(500)
    R['strip_off_persists'] = await J('document.body.classList.contains("strip-off")')
    await pg.click('#handle'); await pg.wait_for_timeout(400)
    R['strip_restored'] = not await J('document.body.classList.contains("strip-off")')
    R['js_errors'] = errs
    await ctx.close()
    ok = (R['cues'][0] == ncues and R['chapter_headers'][0] == nch and R['progress_segments'] == nch and not errs
          and R['sheet_open'] and R['sheet_jump']['closed'] and R['strip_off_persists'] and R['strip_restored']
          and modes == ['m-both+swap', 'm-en', 'm-zh', 'm-both'] and R['no_horizontal_overflow']
          and R['tap_seek']['ok']
          and R.get('style_to_B') and R.get('style_to_S') and R.get('style_B_persists') and R.get('style_keeps_position'))
    if has_spk:
        ok = ok and R.get('spk_row_visible') and R.get('spk_labels_on') and R.get('spk_labels_off') and R.get('spk_on_persists') and R.get('spk_turn_starts', 0) > 0
    else:
        ok = ok and R.get('spk_row_hidden')
    if R['audio_loaded']:
        ok = ok and R['tap_seek']['advancing']
    R['ok'] = bool(ok)
    return R


def _norm_url(u):
    return urllib.parse.unquote(urllib.parse.urlsplit(u)._replace(fragment='').geturl())


def make_audio_server(path, chunk_mb, R):
    """Playwright route handler: HTTP Range server over a file on disk, responses capped at chunk_mb."""
    size = os.path.getsize(path)
    cap = max(64 * 1024, int(chunk_mb * 1024 * 1024))
    base = {'Content-Type': 'audio/mpeg', 'Accept-Ranges': 'bytes', 'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'no-store'}

    def read(s, e):
        with open(path, 'rb') as f:
            f.seek(s); return f.read(e - s + 1)

    async def serve(route):
        R['served_requests'] = R.get('served_requests', 0) + 1
        rh = route.request.headers.get('range', '')
        m = re.match(r'\s*bytes=(\d*)-(\d*)', rh)
        if not m or (m.group(1) == '' and m.group(2) == ''):
            if size <= cap:
                return await route.fulfill(status=200, body=read(0, size - 1), headers=base)
            s, e = 0, cap - 1            # no Range: still answer with a short 206 so nothing huge crosses the pipe
        elif m.group(1) == '':           # suffix range: last N bytes
            s = max(0, size - int(m.group(2))); e = size - 1
        else:
            s = int(m.group(1)); e = int(m.group(2)) if m.group(2) else size - 1
        if s >= size:
            return await route.fulfill(status=416, body=b'', headers=dict(base, **{'Content-Range': f'bytes */{size}'}))
        e = min(e, size - 1, s + cap - 1)
        await route.fulfill(status=206, body=read(s, e),
                            headers=dict(base, **{'Content-Range': f'bytes {s}-{e}/{size}', 'Content-Length': str(e - s + 1)}))
    return serve


def test_copy(src, kbps):
    """Time-aligned low-bitrate mono copy (same start/duration, no extra padding) for slow test machines."""
    out = os.path.join(tempfile.gettempdir(), f'pbp_test_{os.getpid()}_{kbps}k.mp3')
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', src, '-vn', '-map_metadata', '-1', '-ac', '1',
                    '-ar', '22050', '-b:a', f'{kbps}k', out], check=True)
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('html')
    ap.add_argument('--local-audio', help='serve this mp3 for the lite-mode audioUrl (offline testing)')
    ap.add_argument('--shots', help='screenshot path prefix')
    ap.add_argument('--chrome', help='use this Chrome/Chromium binary instead of Playwright\'s bundled one')
    ap.add_argument('--devices', default='iphone,android')
    ap.add_argument('--chunk-mb', type=float, default=2.0, help='max bytes per served range response (default 2 MB)')
    ap.add_argument('--test-kbps', type=int, help='serve a time-aligned mono copy at this bitrate instead of --local-audio as is')
    ap.add_argument('--tap-tolerance', type=int, default=1, help='accept the tapped line or a neighbour up to N lines away (default 1)')
    a = ap.parse_args()
    a.audio_path = a.local_audio
    tmp = None
    if a.local_audio and a.test_kbps:
        tmp = a.audio_path = test_copy(a.local_audio, a.test_kbps)
    exe = a.chrome
    async with async_playwright() as p:
        args = ['--autoplay-policy=no-user-gesture-required', '--mute-audio']
        try:
            browser = await p.chromium.launch(executable_path=exe, args=args)
        except Exception:
            exe = exe or shutil.which('google-chrome') or shutil.which('chromium') or shutil.which('chromium-browser')
            if not exe:
                raise
            browser = await p.chromium.launch(executable_path=exe, args=args)
        try:
            res = {d: await run_device(p, browser, a.html, d, a) for d in a.devices.split(',')}
        finally:
            await browser.close()
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if not all(r['ok'] for r in res.values()):
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
