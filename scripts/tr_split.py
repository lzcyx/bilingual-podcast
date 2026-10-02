#!/usr/bin/env python3
"""Split the final lines into translation blocks for the agent (the ｜ protocol).

  python tr_split.py --workdir work [--block 100] [--lang zh]

Reads work/lines.json, writes work/tr/bNN.src.txt. Consecutive lines that form one sentence are grouped:
    12-14<TAB>I think the big thing | about this game is | that it just keeps going.
The agent writes work/tr/bNN.<lang>.txt with the SAME keys, translating each group as one natural sentence in
context and putting exactly one ｜ where each " | " is (so a group of 3 lines has 2 ｜ and 3 parts):
    12-14<TAB>我觉得这款游戏最大的亮点｜在于｜它一直都有新东西。
Single lines have no ｜. Lines starting with # are comments (context from the previous block).
When speakers.json / lines have speaker ids, a "# speaker: Name" comment is added at each speaker turn so the
translator knows who is talking. Groups never cross a speaker boundary.
"""
import argparse, json, os, re

END = re.compile(r'[.?!…]["\')\]]?$')


def load_speakers(wd):
    p = os.path.join(wd, 'speakers.json')
    if not os.path.exists(p):
        return {}
    raw = json.load(open(p, encoding='utf-8'))
    out = {}
    for k, v in raw.items():
        if isinstance(v, str):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = v.get('name') or k
        else:
            out[k] = str(v)
    return out


def spk_label(spk, names):
    if not spk:
        return ''
    return names.get(spk) or spk


def groups(lines, max_lines=6, gap_s=2.0):
    out, cur = [], []
    for k, c in enumerate(lines):
        cur.append(c)
        nxt = lines[k + 1] if k + 1 < len(lines) else None
        spk_break = nxt is not None and c.get('spk') and nxt.get('spk') and c.get('spk') != nxt.get('spk')
        if (END.search(c['en']) or len(cur) >= max_lines or nxt is None
                or nxt['s'] - c['e'] > gap_s or spk_break):
            out.append(cur); cur = []
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', default='work')
    ap.add_argument('--block', type=int, default=100, help='approx. lines per block file')
    ap.add_argument('--lang', default='zh', help='target language code (only used in the header/filename hint)')
    a = ap.parse_args()
    lines = json.load(open(os.path.join(a.workdir, 'lines.json'), encoding='utf-8'))
    assert all(c['i'] == k for k, c in enumerate(lines)), 'lines.json indexes must be 0..n-1 (run apply_edits.py)'
    names = load_speakers(a.workdir)
    td = os.path.join(a.workdir, 'tr'); os.makedirs(td, exist_ok=True)
    for f in os.listdir(td):
        if f.endswith('.src.txt'):
            os.remove(os.path.join(td, f))
    G = groups(lines)
    blocks, cur = [], []
    for g in G:
        cur.append(g)
        if sum(len(x) for x in cur) >= a.block:
            blocks.append(cur); cur = []
    if cur:
        blocks.append(cur)
    prev_tail = []
    for b, blk in enumerate(blocks, 1):
        lo, hi = blk[0][0]['i'], blk[-1][-1]['i']
        p = os.path.join(td, f'b{b:02d}.src.txt')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(f'# block {b:02d}/{len(blocks):02d} · lines {lo}-{hi} · write tr/b{b:02d}.{a.lang}.txt with the same keys; '
                    f'one ｜ per " | "\n')
            for c in prev_tail:
                lab = spk_label(c.get('spk'), names)
                prefix = f'[{lab}] ' if lab else ''
                f.write(f"# context: {prefix}{c['en']}\n")
            last_spk = None
            for g in blk:
                spk = g[0].get('spk')
                if spk and spk != last_spk:
                    f.write(f'# speaker: {spk_label(spk, names)} ({spk})\n')
                    last_spk = spk
                key = str(g[0]['i']) if len(g) == 1 else f"{g[0]['i']}-{g[-1]['i']}"
                f.write(key + '\t' + ' | '.join(c['en'].replace('|', '/') for c in g) + '\n')
        prev_tail = [c for g in blk[-2:] for c in g][-3:]
    print(f'{len(lines)} lines, {len(G)} groups -> {len(blocks)} blocks in {td}/ (b01.src.txt … b{len(blocks):02d}.src.txt)')


if __name__ == '__main__':
    main()
