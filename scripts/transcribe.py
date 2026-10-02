#!/usr/bin/env python3
"""Chunked Whisper transcription with word timestamps (faster-whisper, CPU friendly, resumable).

  python transcribe.py episode.mp3 --workdir work --prompt-file work/prompt.txt
  python transcribe.py episode.mp3 --workdir clip --start 600 --duration 60      # quick test on a clip

Pipeline: ffmpeg -> 16 kHz mono wav -> cut into ~120 s chunks at the quietest point near each boundary
-> N worker processes (each loads the model once) transcribe chunks -> work/chunks/kNN.json
-> merged work/words.json  [{"s":..,"e":..,"text":..,"words":[[s,e,"word"],..]}, ..]  (absolute seconds)

Why chunks: long single-pass decoding on CPU tended to skip or garble whole stretches; 2-minute chunks with
no_speech/log_prob thresholds disabled keep every chunk transcribed, allow parallelism and resuming.
Re-running skips chunks whose kNN.json exists (delete a file to redo that chunk).
"""
import argparse, json, multiprocessing as mp, os, subprocess, sys, time, wave

import numpy as np


def to_wav(src, dst, start=None, duration=None):
    cmd = ['ffmpeg', '-y', '-loglevel', 'error']
    if start is not None: cmd += ['-ss', str(start)]
    if duration is not None: cmd += ['-t', str(duration)]
    cmd += ['-i', src, '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', dst]
    subprocess.run(cmd, check=True)


def read_wav(path):
    with wave.open(path, 'rb') as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def plan_chunks(x, sr=16000, target=120.0, search=6.0, win=0.5, hop=0.1):
    """Boundaries every ~target seconds, each moved to the lowest-energy 0.5 s window within ±search s."""
    dur = len(x) / sr
    f = x.astype(np.float32)
    n = int(win * sr); h = int(hop * sr)
    if len(f) < n: return [(0.0, round(dur, 2))]
    frames = np.lib.stride_tricks.sliding_window_view(f, n)[::h]
    rms = np.sqrt((frames ** 2).mean(axis=1))
    centers = np.arange(len(rms)) * hop + win / 2
    cuts = [0.0]; t = target
    while t < dur - 30:
        m = (centers >= t - search) & (centers <= t + search)
        idx = np.where(m)[0]
        c = round(float(centers[idx[np.argmin(rms[idx])]]), 2) if len(idx) else round(t, 2)
        cuts.append(c); t = c + target
    cuts.append(round(dur, 2))
    return [(cuts[k], cuts[k + 1]) for k in range(len(cuts) - 1)]


def worker(part, nparts, args, chunks, cdir, prompt, offset=0.0):
    from faster_whisper import WhisperModel
    m = WhisperModel(args.model, device='cpu', compute_type=args.compute_type, cpu_threads=args.threads,
                     download_root=args.model_dir)
    for k, (a0, b) in enumerate(chunks):
        if k % nparts != part: continue
        a = a0 + offset  # absolute time in the original audio
        out = os.path.join(cdir, f'k{k:02d}.json')
        if os.path.exists(out): continue
        t0 = time.time()
        segs, _ = m.transcribe(os.path.join(cdir, f'k{k:02d}.wav'), language=args.lang, beam_size=args.beam,
                               word_timestamps=True, no_speech_threshold=None, log_prob_threshold=None,
                               initial_prompt=prompt or None)
        res = [{'s': round(s.start + a, 3), 'e': round(s.end + a, 3), 'text': s.text.strip(),
                'words': [[round(w.start + a, 3), round(w.end + a, 3), w.word] for w in (s.words or [])]} for s in segs]
        json.dump(res, open(out + '.tmp', 'w'), ensure_ascii=False)
        os.replace(out + '.tmp', out)
        nw = sum(len(r['words']) for r in res)
        print(f'[worker {part}] chunk {k:02d} {a:7.1f}-{b + offset:7.1f}s  {len(res)} segs {nw} words  {time.time() - t0:.0f}s', flush=True)


def main():
    cpu = os.cpu_count() or 4
    ap = argparse.ArgumentParser()
    ap.add_argument('audio')
    ap.add_argument('--workdir', default='work')
    ap.add_argument('--model', default='large-v3-turbo', help='faster-whisper model name or local path')
    ap.add_argument('--model-dir', default=None, help='download/cache dir (default: HF cache)')
    ap.add_argument('--lang', default='en', help='source language code')
    ap.add_argument('--prompt', default='', help='initial prompt: show name, host names, key titles (spelling hints)')
    ap.add_argument('--prompt-file')
    ap.add_argument('--chunk', type=float, default=120.0, help='target chunk length (s)')
    ap.add_argument('--jobs', type=int, default=max(1, min(4, cpu // 4)))
    ap.add_argument('--threads', type=int, default=0, help='cpu threads per job (default cpu/jobs)')
    ap.add_argument('--compute-type', default='float32', help='float32 (proven) | int8 (faster, slightly worse)')
    ap.add_argument('--beam', type=int, default=5)
    ap.add_argument('--start', type=float, help='only transcribe from this second (testing)')
    ap.add_argument('--duration', type=float, help='only transcribe this many seconds (testing)')
    args = ap.parse_args()
    user_threads = bool(args.threads)
    args.threads = args.threads or max(1, cpu // args.jobs)
    prompt = open(args.prompt_file, encoding='utf-8').read().strip() if args.prompt_file else args.prompt

    wd = args.workdir; cdir = os.path.join(wd, 'chunks'); os.makedirs(cdir, exist_ok=True)
    wav = os.path.join(wd, 'audio16k.wav')
    if not os.path.exists(wav):
        print('converting to 16 kHz mono wav …', flush=True); to_wav(args.audio, wav, args.start, args.duration)
    offset = args.start or 0.0
    json.dump({'source': os.path.abspath(args.audio), 'offset': offset, 'model': args.model, 'lang': args.lang},
              open(os.path.join(wd, 'transcribe_meta.json'), 'w'))
    plan = os.path.join(cdir, 'chunks.json')
    if os.path.exists(plan):
        chunks = [tuple(c) for c in json.load(open(plan))]
    else:
        chunks = plan_chunks(read_wav(wav), target=args.chunk)
        json.dump(chunks, open(plan, 'w'))
    for k, (a, b) in enumerate(chunks):
        p = os.path.join(cdir, f'k{k:02d}.wav')
        if not os.path.exists(p):
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-ss', str(a), '-t', str(b - a), '-i', wav, p], check=True)
    todo = [k for k in range(len(chunks)) if not os.path.exists(os.path.join(cdir, f'k{k:02d}.json'))]
    total = chunks[-1][1]
    t0 = time.time()
    if todo:
        nparts = min(args.jobs, len(todo))
        if nparts < args.jobs and not user_threads: args.threads = max(1, cpu // nparts)
        print(f'{len(chunks)} chunks, {total / 60:.1f} min audio, {len(todo)} to do; model={args.model} '
              f'{args.compute_type} jobs={nparts} threads/job={args.threads}', flush=True)
        if nparts == 1:
            worker(0, 1, args, chunks, cdir, prompt, offset)
        else:
            ctx = mp.get_context('spawn')
            ps = [ctx.Process(target=worker, args=(p, nparts, args, chunks, cdir, prompt, offset)) for p in range(nparts)]
            for p in ps: p.start()
            for p in ps: p.join()
            if any(p.exitcode for p in ps): sys.exit('a worker failed; re-run to resume')
    missing = [k for k in range(len(chunks)) if not os.path.exists(os.path.join(cdir, f'k{k:02d}.json'))]
    if missing: sys.exit(f'missing chunks {missing}; re-run to resume')
    merged = []
    for k in range(len(chunks)):
        for sg in json.load(open(os.path.join(cdir, f'k{k:02d}.json'))):
            sg['k'] = k
            merged.append(sg)
    json.dump(merged, open(os.path.join(wd, 'words.json'), 'w'), ensure_ascii=False)
    nw = sum(len(s['words']) for s in merged)
    empty = [k for k in range(len(chunks)) if not json.load(open(os.path.join(cdir, f'k{k:02d}.json')))]
    print(f'done: {len(merged)} segments, {nw} words -> {wd}/words.json  ({time.time() - t0:.0f}s this run)'
          + (f'  WARNING empty chunks: {empty}' if empty else ''), flush=True)


if __name__ == '__main__':
    main()
