#!/usr/bin/env python3
"""Extract a theme colour from cover art and derive the S (Spotify-style) / B (Apple-style) palettes.

CLI:  python theme.py cover.jpg            -> prints the dominant colour and both palettes
      python theme.py cover.jpg --hex '#2d749a'  (override: treat this hex as the dominant colour)
"""
import colorsys, sys, argparse
from PIL import Image


def _lum(rgb):
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = [f(x) for x in rgb]
    return .2126 * r + .7152 * g + .0722 * b


def contrast_white(rgb):
    return 1.05 / (_lum(rgb) + 0.05)


def hexs(rgb):
    return '#%02x%02x%02x' % tuple(round(max(0, min(1, x)) * 255) for x in rgb)


def parse_hex(h):
    h = h.lstrip('#')
    return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def dominant(img):
    """Most 'characterful' colour of the cover: frequent AND saturated, not near-black/white.
    Returns (rgb, (h, l, s)). Falls back to the average colour for grey/monochrome art."""
    im = img.convert('RGB').resize((150, 150))
    q = im.quantize(colors=10, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    best = None
    for n, i in q.getcolors():
        rgb = [c / 255 for c in pal[3 * i:3 * i + 3]]
        h, l, s = colorsys.rgb_to_hls(*rgb)
        if l < 0.12 or l > 0.85 or s < 0.1:
            continue
        sc = n * (s + 0.1)
        if not best or sc > best[0]:
            best = (sc, rgb, (h, l, s))
    if best is None:  # monochrome cover: use mean colour, keep it muted
        px = list(im.getdata())
        rgb = [sum(p[k] for p in px) / len(px) / 255 for k in range(3)]
        h, l, s = colorsys.rgb_to_hls(*rgb)
        return rgb, (h, l, s)
    return best[1], best[2]


def deep(h, s, target=7.2, smin=0.62):
    """Darken the hue until white text reaches `target` contrast (used by variant B)."""
    s = max(s, smin)
    l = 0.5
    while l > 0.05:
        rgb = colorsys.hls_to_rgb(h, l, s)
        if contrast_white(rgb) >= target:
            return rgb
        l -= 0.005
    return colorsys.hls_to_rgb(h, 0.05, s)


def palette(variant, h, s, grey=False):
    """Colour placeholders for templates/player_<variant>.html."""
    if grey:  # keep monochrome covers monochrome-ish
        s = min(s, 0.12)
        smin_top = smin_card = s
        smin_deep = s
    else:
        smin_top, smin_card, smin_deep = 0.58, 0.55, 0.62
    if variant == 'S':
        top = colorsys.hls_to_rgb(h, 0.47, max(s, smin_top))
        card = colorsys.hls_to_rgb(h, 0.39, max(s, smin_card))
        return {'__C_BASE__': hexs(card), '__C_TOP__': hexs(top)}
    d = deep(h, s, smin=smin_deep)
    hh, ll, ss = colorsys.rgb_to_hls(*d)
    return {'__C_BASE__': hexs(d),
            '__C_BAR__': hexs(colorsys.hls_to_rgb(hh, max(ll - 0.045, 0.04), ss)),
            '__C_SHEET__': hexs(colorsys.hls_to_rgb(hh, max(ll - 0.03, 0.04), ss))}


def theme_from(img=None, override_hex=None):
    if override_hex:
        rgb = parse_hex(override_hex)
        h, l, s = colorsys.rgb_to_hls(*rgb)
    else:
        rgb, (h, l, s) = dominant(img)
    grey = s < 0.1
    return {'dominant': hexs(rgb), 'S': palette('S', h, s, grey), 'B': palette('B', h, s, grey)}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cover')
    ap.add_argument('--hex')
    a = ap.parse_args()
    print(theme_from(Image.open(a.cover), a.hex))
