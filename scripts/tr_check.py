#!/usr/bin/env python3
"""Verify the agent's translation blocks and merge them into the final cues.

  python tr_check.py --workdir work [--lang zh] [--out work/cues.json] [--force]

For every tr/bNN.src.txt expects tr/bNN.<lang>.txt with the same keys in any order. Checks: missing / extra /
duplicate keys, ｜ part count == line count of the group, empty parts, parts identical to the English (untranslated),
Latin-only parts when the target is CJK, suspicious length ratios. Errors block the merge (exit 1) unless --force;
warnings are printed for review. On success writes cues.json [{"s","e","en","tr", optional "spk"}] for build.py.
"""
import argparse, glob, json, os, re, sys

CJK = re.compile(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]')


def parse(path):
    d, dup = {}, []
    for n, ln in enumerate(open(path, encoding='utf-8').read().splitlines(), 1):
        if not ln.strip() or ln.lstrip().startswith('#'): continue
        if '\t' not in ln:
            m = re.match(r'^(\d+(?:-\d+)?)\s+(.*)$', ln)
            if not m: raise ValueError(f'{path}:{n}: no key/tab: {ln[:60]}')
            k, v = m.groups()
        else:
            k, v = ln.split('\t', 1)
        k = k.strip()
        if k in d: dup.append(k)
        d[k] = v.strip()
    return d, dup


def rng(k):
    a, b = (k.split('-') + [k])[:2]
    return int(a), int(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', default='work')
    ap.add_argument('--lang', default='zh')
    ap.add_argument('--out')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--only', help='check one block only (stem b01 or b01.src.txt) and do not write cues.json')
    a = ap.parse_args()
    lines = json.load(open(os.path.join(a.workdir, 'lines.json'), encoding='utf-8'))
    cjk_target = a.lang.lower()[:2] in ('zh', 'ja', 'ko')
    errs, warns, tr = [], [], {}
    srcs = sorted(glob.glob(os.path.join(a.workdir, 'tr', 'b*.src.txt')))
    if not srcs: sys.exit('no tr/b*.src.txt — run tr_split.py first')
    only = None
    if a.only:
        only = a.only.strip().replace('\\', '/')
        only = os.path.basename(only)
        for suf in ('.src.txt', f'.{a.lang}.txt', '.txt'):
            if only.endswith(suf):
                only = only[:-len(suf)]
                break
        if re.fullmatch(r'\d+', only):
            only = f'b{int(only):02d}'
        srcs = [p for p in srcs if os.path.basename(p) == f'{only}.src.txt']
        if not srcs:
            sys.exit(f'no block matching {a.only}')
    for sp in srcs:
        tp = sp.replace('.src.txt', f'.{a.lang}.txt'); name = os.path.basename(tp)
        no_cjk_lines = []
        src, _ = parse(sp)
        if not os.path.exists(tp):
            errs.append(f'{name}: missing file ({len(src)} groups)'); continue
        try:
            got, dup = parse(tp)
        except ValueError as e:
            errs.append(str(e)); continue
        for k in dup: errs.append(f'{name}: duplicate key {k}')
        for k in sorted(set(src) - set(got), key=lambda x: rng(x)[0]): errs.append(f'{name}: missing group {k}')
        for k in sorted(set(got) - set(src), key=lambda x: rng(x)[0]): errs.append(f'{name}: unexpected key {k} (keys must match the .src.txt)')
        for k, v in got.items():
            if k not in src: continue
            lo, hi = rng(k); need = hi - lo + 1
            parts = [p.strip() for p in v.replace('|', '｜').split('｜')]
            if len(parts) != need:
                errs.append(f'{name}: {k} has {len(parts)} part(s), needs {need} (={need - 1} ｜)'); continue
            en_parts = [p.strip() for p in src[k].split(' | ')]
            for i, p, e in zip(range(lo, hi + 1), parts, en_parts):
                if not p:
                    errs.append(f'{name}: line {i} empty translation')
                elif p == '［未译］':
                    errs.append(f'{name}: line {i} is marked untranslated')
                elif p == e and len(e) > 12:
                    errs.append(f'{name}: line {i} identical to source: {e[:50]}')
                elif cjk_target and not CJK.search(p) and len(e.split()) > 3:
                    warns.append(f'{name}: line {i} has no CJK: {p[:50]}')
                    no_cjk_lines.append(i)
                tr[i] = p
            if cjk_target:
                el, zl = len(src[k]), len(v)
                if el > 40 and (zl < el * 0.12 or zl > el * 1.2): warns.append(f'{name}: {k} length ratio {zl}/{el} looks off')
        if cjk_target and no_cjk_lines:
            run = []
            runs = []
            for i in sorted(no_cjk_lines):
                if run and i != run[-1] + 1:
                    if len(run) >= 3: runs.append(run)
                    run = []
                run.append(i)
            if len(run) >= 3: runs.append(run)
            for r in runs:
                errs.append(f'{name}: line {r[0]}-{r[-1]} consecutive translations have no CJK')
            translated_lines = sum(rng(k)[1] - rng(k)[0] + 1 for k in src)
            if translated_lines and len(no_cjk_lines) / translated_lines > 0.15:
                for i in no_cjk_lines:
                    errs.append(f'{name}: line {i} contributes to too many no-CJK translations ({len(no_cjk_lines)}/{translated_lines})')
    if not only:
        missing = [i for i in range(len(lines)) if i not in tr]
        if missing and not errs: errs.append(f'lines without translation: {missing[:30]}')
    label = f'block {only}' if only else f'{len(srcs)} blocks'
    denom = len(tr) if only else len(lines)
    print(f'{label}, {len(tr)}/{denom} lines translated, {len(errs)} errors, {len(warns)} warnings')
    for w in warns[:80]: print('  warn:', w)
    for e in errs[:80]: print('  ERROR:', e)
    if errs and not a.force: sys.exit(1)
    if only:
        print(f'block {only} ok')
        return
    cues = []
    for c in lines:
        row = dict(s=c['s'], e=c['e'], en=c['en'], tr=tr.get(c['i'], ''))
        if c.get('spk'):
            row['spk'] = c['spk']
        cues.append(row)
    out = a.out or os.path.join(a.workdir, 'cues.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(cues, f, ensure_ascii=False, indent=0)
    print(f'wrote {out}')


if __name__ == '__main__':
    main()
