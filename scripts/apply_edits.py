#!/usr/bin/env python3
"""Apply proofreading edits to the raw lines and freeze the final numbered line list.

  python apply_edits.py --workdir work            # uses work/edits.json and work/fixes.json if present

edits.json is keyed by the line id from lines_raw.tsv:
  {"03-017": {"en": "corrected full text"},                     # replace text (verbatim, fixes not applied)
   "03-018": {"drop": true},                                    # delete line (noise / hallucination)
   "03-019": {"merge_prev": true},                              # append to previous line (optional "en" = text to append)
   "05-002": {"split": [[6, "first six words."], [0, "rest of the line."]]},
             # split by word counts (0 = all remaining words); a part written [n, "text", "^"] is appended to the
             # previous line instead of becoming a new line. Word counts must cover the line exactly.
             # Optional 4th element or a dict form can set the speaker for that part:
             #   [6, "text", "", "S1"]  or  {"n": 6, "en": "text", "spk": "S1"}
   "12-004": {"spk": "S1"},                                     # override speaker id (kept through cues/build)
  }
Optional work/speakers.json maps raw ids to display names (used later by build / tr_split context):
  {"S0": {"name": "Sid Shuman", "color": "#7eb8ff"}, "S1": {"name": "Tim Turi"}}

Writes work/lines.json [{"i":0,"id":"00-000","s":..,"e":..,"en":..,"spk":"S0"}, ..] and work/lines.tsv
(i, id, m:ss, spk, text). Line indexes i are what translation blocks and chapters refer to. Structural
edits after translating require re-running tr_split.py and redoing the affected blocks.
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from textfix import load_fixes, apply_fixes  # noqa: E402


def _part_fields(part):
    """Normalize a split part to (n, text, merge_up, spk_override)."""
    if isinstance(part, dict):
        return (int(part.get('n', part.get('words', 0))), part.get('en', part.get('text', '')),
                part.get('merge') == '^' or part.get('^') is True,
                part.get('spk'))
    n, txt = part[0], part[1]
    merge = len(part) > 2 and part[2] == '^'
    spk = part[3] if len(part) > 3 and part[3] else None
    return n, txt, merge, spk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', default='work')
    ap.add_argument('--edits'); ap.add_argument('--fixes')
    a = ap.parse_args()
    wd = a.workdir
    ep = a.edits or os.path.join(wd, 'edits.json')
    E = json.load(open(ep, encoding='utf-8')) if os.path.exists(ep) else {}
    fixes = load_fixes(a.fixes or os.path.join(wd, 'fixes.json'))
    raw = json.load(open(os.path.join(wd, 'lines_raw.json'), encoding='utf-8'))
    ids = {c['id'] for c in raw}
    unknown = sorted(set(E) - ids)
    if unknown:
        sys.exit(f'edits refer to unknown ids: {unknown[:20]}')
    out = []
    for c in raw:
        e = E.get(c['id'])
        base_spk = c.get('spk')
        if e is None:
            row = dict(id=c['id'], s=c['s'], e=c['e'], en=apply_fixes(c['en'], fixes))
            if base_spk:
                row['spk'] = base_spk
            out.append(row)
        elif e.get('drop'):
            continue
        elif 'split' in e:
            w = c['w']; pos = 0
            for q, part in enumerate(e['split']):
                n, txt, merge, spk_ov = _part_fields(part)
                seg = w[pos:pos + n] if n > 0 else w[pos:]
                if not seg:
                    sys.exit(f"{c['id']}: split part {q} has no words left")
                spk = spk_ov or base_spk
                if merge:
                    if not out:
                        sys.exit(f"{c['id']}: nothing to merge into")
                    out[-1]['e'] = seg[-1][1]
                    out[-1]['en'] = (out[-1]['en'] + ' ' + txt).strip()
                    if spk and not out[-1].get('spk'):
                        out[-1]['spk'] = spk
                else:
                    row = dict(id=c['id'] + 'abcdefghij'[q], s=seg[0][0], e=seg[-1][1], en=txt)
                    if spk:
                        row['spk'] = spk
                    out.append(row)
                pos += len(seg)
            if pos != len(w):
                sys.exit(f"{c['id']}: split covers {pos} of {len(w)} words")
        elif e.get('merge_prev'):
            if not out:
                sys.exit(f"{c['id']}: nothing to merge into")
            out[-1]['e'] = c['e']
            out[-1]['en'] = (out[-1]['en'] + ' ' + e.get('en', apply_fixes(c['en'], fixes))).strip()
            if e.get('spk'):
                out[-1]['spk'] = e['spk']
        else:
            row = dict(id=c['id'], s=c['s'], e=c['e'],
                       en=e.get('en', apply_fixes(c['en'], fixes)))
            spk = e.get('spk', base_spk)
            if spk:
                row['spk'] = spk
            out.append(row)
            # allow {"spk":"S1"} alone (no en) — already handled via e.get('spk', base_spk)
    for x, y in zip(out, out[1:]):
        if x['e'] > y['s']:
            x['e'] = y['s']
    final = []
    for i, c in enumerate(out):
        row = dict(i=i, id=c['id'], s=round(c['s'], 2), e=round(c['e'], 2), en=c['en'].strip())
        if c.get('spk'):
            row['spk'] = c['spk']
        final.append(row)
    with open(os.path.join(wd, 'lines.json'), 'w', encoding='utf-8') as f:
        json.dump(final, f, ensure_ascii=False, indent=0)
    with open(os.path.join(wd, 'lines.tsv'), 'w', encoding='utf-8') as f:
        for c in final:
            spk = c.get('spk') or ''
            f.write(f"{c['i']}\t{c['id']}\t{int(c['s'] // 60)}:{c['s'] % 60:05.2f}\t{spk}\t{c['en']}\n")
    nspk = len({c.get('spk') for c in final if c.get('spk')})
    print(f'{len(raw)} raw lines, {len(E)} edits -> {len(final)} final lines'
          + (f' ({nspk} speakers)' if nspk else '')
          + f' -> {wd}/lines.json, lines.tsv')


if __name__ == '__main__':
    main()
