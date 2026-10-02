#!/usr/bin/env python3
"""Turn word timestamps (transcribe.py output) into subtitle lines.

  python segment.py --workdir work [--fixes work/fixes.json]

Reads  work/words.json  (segments with "k" = chunk index and "words" = [[s,e,word],..])
Optional work/diarization.json [{"s","e","spk":0}, ...] — assigns each word a speaker and
breaks a line whenever the speaker changes (in addition to the pause/sentence rules).
Writes work/lines_raw.json  [{"id":"03-017","s":..,"e":..,"en":"text","spk":"S0","w":[[s,e,word],..]}, ..]
       work/lines_raw.tsv   id<TAB>m:ss<TAB>[S0]<TAB>text   (for proofreading; ids are stable per chunk)

Line-break rules (tuned on real podcast chatter): break on a pause > 1.5 s; at sentence end once the line has
>= 6 words or >= 2.5 s; at a comma once >= 16 words; hard cap 26 words (cut back to the last comma/sentence end);
always break when the speaker changes. 1–2-word fragments are merged into the previous line when that line has
no sentence end, the gap is < 1 s, and the speaker is the same.
"""
import argparse, json, os, re, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from textfix import load_fixes, apply_fixes  # noqa: E402

END = re.compile(r'[.?!…]["\')\]]?$')


def load_diar(path):
    if not path or not os.path.exists(path):
        return None
    segs = json.load(open(path, encoding='utf-8'))
    # sorted for binary-ish scan
    segs = sorted(segs, key=lambda x: (x['s'], x['e']))
    return segs


def speaker_at(segs, s, e):
    """Speaker for a word [s, e]: the speaker with the most overlap; else the nearest segment within 0.5 s."""
    if not segs:
        return None
    ov = {}
    for sg in segs:
        if sg['s'] > e:
            break
        o = min(e, sg['e']) - max(s, sg['s'])
        if o > 0:
            ov[sg['spk']] = ov.get(sg['spk'], 0.0) + o
    if ov:
        return f"S{int(max(ov, key=ov.get))}"
    best, best_d = None, 0.5
    for sg in segs:
        d = max(sg['s'] - e, s - sg['e'], 0.0)
        if d < best_d:
            best, best_d = sg, d
    return f"S{int(best['spk'])}" if best is not None else None


def fix_boundaries(words, end_re=None):
    """Cheap fixes for words that land on the wrong side of a speaker change (segmentation boundaries are
    ~0.1-0.3 s off). At a change A|B:
      - if A's last word has no sentence end and B's first sentence is only 1-2 words ("…we have a | count."),
        those words go to A;
      - if A's run ends with 1-2 words that start after a pause > 1 s and B follows within 0.3 s
        ("…after that. <pause> Hey | guys, we got…"), those words go to B."""
    end_re = end_re or END
    n = len(words)
    for i in range(n - 1):
        a, b = words[i], words[i + 1]
        if not a.get('spk') or not b.get('spk') or a['spk'] == b['spk']:
            continue
        # rule 1: short sentence tail spilled into B
        if not end_re.search(a['w']):
            j = i + 1
            while j < n and j - i <= 2 and words[j].get('spk') == b['spk'] and not end_re.search(words[j]['w']):
                j += 1
            if j < n and j - i <= 2 and words[j].get('spk') == b['spk'] and end_re.search(words[j]['w']):
                nxt = words[j + 1] if j + 1 < n else None
                if nxt is None or nxt.get('spk') == b['spk']:
                    for q in range(i + 1, j + 1):
                        words[q]['spk'] = a['spk']
                    continue
        # rule 2: utterance start glued to the previous speaker
        k = i
        while k > 0 and i - k < 2 and words[k - 1].get('spk') == a['spk'] and words[k]['s'] - words[k - 1]['e'] <= 1.0:
            k -= 1
        gap_before = words[k]['s'] - words[k - 1]['e'] if k > 0 else 99
        if i - k < 2 and gap_before > 1.0 and b['s'] - a['e'] < 0.3:
            for q in range(k, i + 1):
                words[q]['spk'] = b['spk']
    return words


def seg_words(words, fixes, diar=None, gap_s=1.5, min_words=6, min_dur=2.5, comma_words=16, max_words=26):
    lines, cur = [], []

    def flush():
        if cur:
            spk = cur[0].get('spk')
            lines.append({'s': round(cur[0]['s'], 2), 'e': round(cur[-1]['e'], 2),
                          'en': apply_fixes(' '.join(x['w'] for x in cur), fixes),
                          'spk': spk,
                          'w': [[round(x['s'], 2), round(x['e'], 2), x['w']] for x in cur]})
        cur.clear()

    if diar is not None:
        for wd in words:
            if wd.get('spk') is None:
                wd['spk'] = speaker_at(diar, wd['s'], wd['e'])
        fix_boundaries(words)
    for wd in words:
        if cur:
            prev = cur[-1]
            spk_change = (wd.get('spk') is not None and prev.get('spk') is not None
                          and wd['spk'] != prev['spk'])
            if spk_change:
                flush()
            elif wd['s'] - prev['e'] > gap_s:
                flush()
            elif END.search(prev['w']) and (len(cur) >= min_words or prev['e'] - cur[0]['s'] >= min_dur):
                flush()
            elif len(cur) >= comma_words and prev['w'].endswith(','):
                flush()
            elif len(cur) >= max_words:
                j = max((q for q in range(4, len(cur) - 2) if END.search(cur[q - 1]['w'])
                         or (q >= 6 and re.search(r'[,;]$', cur[q - 1]['w']))), default=None)
                if j:
                    rest = cur[j:]; del cur[j:]; flush(); cur.extend(rest)
                else:
                    flush()
        cur.append(wd)
    flush()
    out = []
    for c in lines:
        same_spk = (out and c.get('spk') == out[-1].get('spk'))
        if (out and same_spk and len(c['en'].split()) <= 2 and not END.search(out[-1]['en'])
                and c['s'] - out[-1]['e'] < 1.0):
            out[-1]['en'] = apply_fixes(out[-1]['en'] + ' ' + c['en'], fixes)
            out[-1]['e'] = c['e']
            out[-1]['w'] += c['w']
        else:
            out.append(c)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', default='work')
    ap.add_argument('--words', help='default: <workdir>/words.json')
    ap.add_argument('--fixes', help='regex fixes JSON (default: <workdir>/fixes.json if present)')
    ap.add_argument('--diarization', help='default: <workdir>/diarization.json if present')
    a = ap.parse_args()
    wd = a.workdir
    fixes = load_fixes(a.fixes or os.path.join(wd, 'fixes.json'))
    diar_path = a.diarization or os.path.join(wd, 'diarization.json')
    diar = load_diar(diar_path)
    segs = json.load(open(a.words or os.path.join(wd, 'words.json'), encoding='utf-8'))
    by_k = defaultdict(list)
    for sg in segs:
        for s, e, w in sg['words']:
            if w.strip():
                by_k[sg.get('k', 0)].append({'s': s, 'e': e, 'w': w.strip()})
    lines = []
    for k in sorted(by_k):
        for j, c in enumerate(seg_words(by_k[k], fixes, diar=diar)):
            row = dict(id=f'{k:02d}-{j:03d}', **c)
            if row.get('spk') is None:
                row.pop('spk', None)
            lines.append(row)
    with open(os.path.join(wd, 'lines_raw.json'), 'w', encoding='utf-8') as f:
        json.dump(lines, f, ensure_ascii=False)
    with open(os.path.join(wd, 'lines_raw.tsv'), 'w', encoding='utf-8') as f:
        for c in lines:
            spk = c.get('spk') or ''
            f.write(f"{c['id']}\t{int(c['s'] // 60)}:{c['s'] % 60:05.2f}\t{spk}\t{c['en']}\n")
    nw = sum(len(c['w']) for c in lines)
    nspk = len({c.get('spk') for c in lines if c.get('spk')})
    print(f'{nw} words -> {len(lines)} lines ({len(by_k)} chunks'
          + (f', {nspk} speakers from {diar_path}' if diar else ', no diarization')
          + f') -> {wd}/lines_raw.json, lines_raw.tsv')


if __name__ == '__main__':
    main()
