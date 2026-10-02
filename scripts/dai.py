#!/usr/bin/env python3
"""Detect podcast dynamic ad insertion (DAI) for an episode audio URL.

DAI hosts stitch ads into the mp3 per request (by region, time, device, listener). A transcript made from ONE
download is then only in sync with that exact file: a listener streaming the same URL elsewhere (e.g. from
China) gets different / longer / no pre-rolls and mid-rolls, so every subtitle after an ad drifts.
=> for DAI feeds the player should embed the audio we transcribed (audio_mode "offline").

  python dai.py <audio url> [--no-network]      # prints JSON {"dynamic_ads": …, "chain": […], "reasons": […]}

Used by fetch_episode.py (probe with redirects) and build.py (URL-string check only, no network).
"""
import argparse, json, re, sys, urllib.error, urllib.parse, urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36'}

# Hosts that insert ads dynamically (domain suffix -> label)
DAI_HOSTS = {
    'megaphone.fm': 'Megaphone', 'art19.com': 'ART19', 'simplecast.com': 'Simplecast (AdsWizz)',
    'simplecastaudio.com': 'Simplecast (AdsWizz)', 'adswizz.com': 'AdsWizz', 'deliveryimages.acm.adswizz.com': 'AdsWizz',
    'acast.com': 'Acast', 'acastcdn.com': 'Acast', 'omny.fm': 'Omny Studio', 'omnycontent.com': 'Omny Studio',
    'spreaker.com': 'Spreaker', 'audioboom.com': 'Audioboom', 'anchor.fm': 'Spotify for Podcasters / Anchor',
    'podcasters.spotify.com': 'Spotify for Podcasters', 'spotifycdn.com': 'Spotify', 'iheart.com': 'iHeart',
    'ihrcdn.com': 'iHeart', 'podbean.com': None, 'flightcast.com': 'Flightcast',
    'captivate.fm': None, 'redcircle.com': 'RedCircle', 'redcircleapi.com': 'RedCircle', 'pinecast.net': None,
    'stitcher.com': 'Stitcher/SiriusXM', 'siriusxm.com': 'SiriusXM', 'wondery.com': 'Wondery', 'wondery.fm': 'Wondery',
    'barstoolsports.com': None, 'tritondigital.com': 'Triton', 'streamtheworld.com': 'Triton', 'adtonos.com': 'AdTonos',
}
DAI_HOSTS = {k: v for k, v in DAI_HOSTS.items() if v}
# Measurement / redirect prefixes: not DAI by themselves, but typical in front of DAI hosts (shown in the chain)
TRACKING_HOSTS = {'podtrac.com', 'pdst.fm', 'chtbl.com', 'chrt.fm', 'pscrb.fm', 'podscribe.com', 'mgln.ai',
                  'op3.dev', 'arttrk.com', 'pfx.vpixl.com', 'claritaspod.com', 'prfx.byspotify.com', 'tracking.swap.fm',
                  'dts.podtrac.com', 'verifi.podscribe.com', 'growx.podkite.com', 'podkite.com'}
TEXT_HINTS = [(re.compile(r'megaphone\.fm/adchoices', re.I), 'show notes: megaphone.fm/adchoices'),
              (re.compile(r'acast\.com/privacy|hosted on acast', re.I), 'show notes: Acast'),
              (re.compile(r'art19\.com/privacy', re.I), 'show notes: ART19'),
              (re.compile(r'omnystudio\.com/listener', re.I), 'show notes: Omny Studio'),
              (re.compile(r'iheartpodcastnetwork|iheart\.com/podcast', re.I), 'show notes: iHeart')]
HOST_RE = re.compile(r'(?:^|[/.])((?:[a-z0-9-]+\.)+[a-z]{2,})(?=/|$)', re.I)


def _match(host, table):
    host = (host or '').lower().split(':')[0]
    for suf in table:
        if host == suf or host.endswith('.' + suf):
            return suf
    return None


def hosts_in_url(url):
    """The host plus hosts embedded in the path (podtrac/pscrb-style prefix chains)."""
    p = urllib.parse.urlsplit(url or '')
    out = [p.hostname or '']
    for seg in (p.path or '').split('/'):
        if '.' in seg and re.fullmatch(r'(?:[a-z0-9-]+\.)+[a-z]{2,}', seg, re.I) and not re.search(r'\.(mp3|m4a|aac|ogg|opus|wav)$', seg, re.I):
            out.append(seg.lower())
    return [h for h in out if h]


def classify(hosts, headers=None, text=None):
    dai, track, reasons = [], [], []
    for h in hosts:
        s = _match(h, DAI_HOSTS)
        if s and DAI_HOSTS[s] not in dai:
            dai.append(DAI_HOSTS[s]); reasons.append(f'DAI host {h} ({DAI_HOSTS[s]})')
        elif _match(h, TRACKING_HOSTS) and h not in track:
            track.append(h)
    for hdr in headers or []:
        if str(hdr.get('has-ads', '')).strip() in ('1', 'true', 'yes'):
            reasons.append(f'response header has-ads: {hdr["has-ads"]} from {hdr.get("_host")}')
    if text:
        for rx, why in TEXT_HINTS:
            if rx.search(text):
                reasons.append(why)
    return dict(dynamic_ads=bool(reasons), providers=dai, tracking_prefixes=track, reasons=reasons)


class _Rec(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.hops = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append(dict(url=req.full_url, status=code, headers={k.lower(): v for k, v in headers.items()}))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe(url, timeout=30):
    """Follow redirects with a 2-byte range GET. Returns (chain urls, per-hop headers, final url, error)."""
    rec = _Rec()
    opener = urllib.request.build_opener(rec)
    req = urllib.request.Request(url, headers=dict(UA, Range='bytes=0-1'))
    final, err, last_headers = url, None, {}
    try:
        with opener.open(req, timeout=timeout) as r:
            final = r.geturl(); last_headers = {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        final = e.geturl() or url; err = f'HTTP {e.code}'
    except Exception as e:  # network down, DNS, timeout
        err = f'{type(e).__name__}: {e}'
    chain = [h['url'] for h in rec.hops] + ([final] if final not in [h['url'] for h in rec.hops] else [])
    hdrs = [dict(h['headers'], _host=urllib.parse.urlsplit(h['url']).hostname) for h in rec.hops]
    hdrs.append(dict(last_headers, _host=urllib.parse.urlsplit(final).hostname))
    return chain, hdrs, final, err


def detect(url, text=None, network=True, timeout=30):
    """-> {"dynamic_ads": bool, "providers": [...], "tracking_prefixes": [...], "reasons": [...],
           "chain": [...hosts], "final_url": str|None, "probe_error": str|None}"""
    hosts = hosts_in_url(url)
    chain_urls, hdrs, final, err = ([url], [], None, None)
    if network and url and re.match(r'https?://', url):
        chain_urls, hdrs, final, err = probe(url, timeout)
        for u in chain_urls:
            for h in hosts_in_url(u):
                if h not in hosts:
                    hosts.append(h)
    res = classify(hosts, hdrs, text)
    chain_hosts = []
    for u in chain_urls:
        h = urllib.parse.urlsplit(u).hostname
        if h and (not chain_hosts or chain_hosts[-1] != h):
            chain_hosts.append(h)
    if final:  # drop per-request signed query (session keys) from what we store
        final = urllib.parse.urlsplit(final)._replace(query='', fragment='').geturl()
    res.update(chain=chain_hosts or hosts, final_url=final, probe_error=err, network=bool(network))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('url'); ap.add_argument('--no-network', action='store_true')
    a = ap.parse_args()
    r = detect(a.url, network=not a.no_network)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    sys.exit(0)


if __name__ == '__main__':
    main()
