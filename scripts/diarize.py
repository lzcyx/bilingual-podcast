#!/usr/bin/env python3
"""Speaker diarization with sherpa-onnx (public models, no Hugging Face token).

  python diarize.py --workdir work [--num-speakers 4] [--audio work/episode.mp3]
  python diarize.py --workdir work --threshold 0.85     # auto-detect speaker count

Uses OfflineSpeakerDiarization:
  - segmentation: sherpa-onnx-pyannote-segmentation-3-0 (public GitHub release ONNX)
  - embedding:    nemo_en_titanet_small.onnx (English-friendly; also public)
  - clustering:   FastClustering (--num-speakers, else --threshold)

Models download once into ~/.cache/podcast-bilingual-player/diar/ (override with --model-dir).
Writes work/diarization.json: [{"s":18.29,"e":28.1,"spk":0}, ...]  (spk = 0-based int).
Also writes a short summary to stdout. Cross-platform (Windows / Linux / Docker).
"""
from __future__ import annotations

import argparse, hashlib, json, os, ssl, sys, tarfile, tempfile, time, urllib.request, wave

UA = 'Mozilla/5.0 podcast-bilingual-player/diarize'

# Public GitHub release assets (no gated HF token required).
SEG_URL = ('https://github.com/k2-fsa/sherpa-onnx/releases/download/'
           'speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2')
EMB_URL = ('https://github.com/k2-fsa/sherpa-onnx/releases/download/'
           'speaker-recongition-models/nemo_en_titanet_small.onnx')
SEG_DIRNAME = 'sherpa-onnx-pyannote-segmentation-3-0'
SEG_ONNX = 'model.onnx'
EMB_NAME = 'nemo_en_titanet_small.onnx'


def default_model_dir():
    home = os.path.expanduser('~')
    return os.path.join(home, '.cache', 'podcast-bilingual-player', 'diar')


def download(url, dest, desc=''):
    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 1000:
        return dest
    print(f'downloading {desc or os.path.basename(dest)} …', flush=True)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    # Some environments (corporate proxies) need unverified; prefer default SSL.
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=600, context=ctx) as r, open(dest + '.part', 'wb') as f:
            total = int(r.headers.get('Content-Length') or 0); n = 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b); n += len(b)
                if total:
                    print(f'\r  {n / 1e6:.1f}/{total / 1e6:.1f} MB', end='', flush=True)
    except Exception:
        # fallback without custom context
        with urllib.request.urlopen(req, timeout=600) as r, open(dest + '.part', 'wb') as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
    print()
    os.replace(dest + '.part', dest)
    return dest


def ensure_models(model_dir):
    os.makedirs(model_dir, exist_ok=True)
    seg_dir = os.path.join(model_dir, SEG_DIRNAME)
    seg_path = os.path.join(seg_dir, SEG_ONNX)
    emb_path = os.path.join(model_dir, EMB_NAME)

    # Optional bundled copies: <skill>/models/ (e.g. for offline Docker images).
    alt_seg = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'models', SEG_DIRNAME, SEG_ONNX),
    ]
    alt_emb = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'models', EMB_NAME),
    ]
    if not os.path.exists(seg_path):
        for p in alt_seg:
            if os.path.exists(p):
                os.makedirs(seg_dir, exist_ok=True)
                # hardlink if possible, else copy
                try:
                    os.link(p, seg_path)
                except OSError:
                    import shutil
                    shutil.copy2(p, seg_path)
                print(f'reusing segmentation model: {p}', flush=True)
                break
        else:
            tar = os.path.join(model_dir, 'seg.tar.bz2')
            download(SEG_URL, tar, 'pyannote segmentation')
            with tarfile.open(tar, 'r:bz2') as tf:
                tf.extractall(model_dir)
            if not os.path.exists(seg_path):
                sys.exit(f'segmentation model missing after extract: expected {seg_path}')
    if not os.path.exists(emb_path):
        for p in alt_emb:
            if os.path.exists(p):
                try:
                    os.link(p, emb_path)
                except OSError:
                    import shutil
                    shutil.copy2(p, emb_path)
                print(f'reusing embedding model: {p}', flush=True)
                break
        else:
            download(EMB_URL, emb_path, 'titanet embedding')
    return seg_path, emb_path


def to_16k_mono(src, dst):
    import subprocess
    subprocess.run(
        ['ffmpeg', '-y', '-loglevel', 'error', '-i', src, '-vn', '-ac', '1', '-ar', '16000',
         '-c:a', 'pcm_s16le', dst],
        check=True)


def read_wav_f32(path):
    with wave.open(path, 'rb') as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, 'need 16-bit mono wav'
        sr = w.getframerate()
        import numpy as np
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype('float32') / 32768.0
        return a, sr


def tidy(segs, min_total):
    """Merge clusters with < min_total seconds of speech into the neighbouring speaker (they are mostly
    laughter, music or crosstalk), then renumber speakers 0..n-1 by first appearance."""
    from collections import Counter
    if min_total and min_total > 0 and segs:
        while True:
            dur = Counter()
            for s in segs:
                dur[s['spk']] += s['e'] - s['s']
            small = [k for k, v in dur.items() if v < min_total]
            if not small or len(dur) <= 1:
                break
            k = min(small, key=lambda x: dur[x])
            for i, s in enumerate(segs):
                if s['spk'] != k:
                    continue
                # neighbour with the closest edge that belongs to another speaker
                cand = []
                for j in range(i - 1, -1, -1):
                    if segs[j]['spk'] != k:
                        cand.append((max(0.0, s['s'] - segs[j]['e']), segs[j]['spk'])); break
                for j in range(i + 1, len(segs)):
                    if segs[j]['spk'] != k:
                        cand.append((max(0.0, segs[j]['s'] - s['e']), segs[j]['spk'])); break
                if cand:
                    s['spk'] = min(cand)[1]
    order = {}
    for s in segs:
        order.setdefault(s['spk'], len(order))
    return [dict(s, spk=order[s['spk']]) for s in segs]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--workdir', default='work')
    ap.add_argument('--audio', help='default: <workdir>/episode.mp3 (or audio16k.wav if present)')
    ap.add_argument('--num-speakers', type=int, default=None,
                    help='known speaker count (preferred). Omit to auto-cluster with --threshold.')
    ap.add_argument('--threshold', type=float, default=0.85,
                    help='clustering threshold when --num-speakers is omitted (higher → fewer speakers)')
    ap.add_argument('--min-speaker-seconds', type=float, default=8.0,
                    help='merge clusters with less total speech than this into their neighbours (0 = keep all)')
    ap.add_argument('--model-dir', default=None, help='cache dir for ONNX models')
    ap.add_argument('--threads', type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument('--out', help='default: <workdir>/diarization.json')
    a = ap.parse_args()

    try:
        import numpy as np  # noqa: F401
        import sherpa_onnx
    except ImportError as e:
        sys.exit(f'missing dependency: {e}\n  pip install sherpa-onnx numpy')

    wd = a.workdir
    os.makedirs(wd, exist_ok=True)
    model_dir = a.model_dir or default_model_dir()
    seg_path, emb_path = ensure_models(model_dir)

    wav = os.path.join(wd, 'audio16k.wav')
    src = a.audio
    if src is None:
        for cand in (os.path.join(wd, 'episode.mp3'), os.path.join(wd, 'audio16k.wav')):
            if os.path.exists(cand):
                src = cand
                break
        else:
            sys.exit('no audio: pass --audio or put episode.mp3 / audio16k.wav in workdir')
    if not (src.endswith('.wav') and os.path.abspath(src) == os.path.abspath(wav) and os.path.exists(wav)):
        # (re)build 16 kHz wav for diarization if missing or stale vs source
        need = (not os.path.exists(wav)
                or (os.path.exists(src) and os.path.getmtime(wav) < os.path.getmtime(src)
                    and os.path.abspath(src) != os.path.abspath(wav)))
        if need and os.path.abspath(src) != os.path.abspath(wav):
            print(f'converting {src} → 16 kHz mono …', flush=True)
            to_16k_mono(src, wav)
        elif not os.path.exists(wav):
            to_16k_mono(src, wav)

    samples, sr = read_wav_f32(wav)
    print(f'audio {len(samples) / sr / 60:.1f} min @ {sr} Hz', flush=True)

    n_spk = a.num_speakers if a.num_speakers and a.num_speakers > 0 else -1
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg_path),
            num_threads=a.threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb_path, num_threads=a.threads),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=n_spk, threshold=a.threshold),
        min_duration_on=0.3, min_duration_off=0.5)
    if not cfg.validate():
        sys.exit('invalid sherpa-onnx config (check model paths)')

    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    if sr != sd.sample_rate:
        sys.exit(f'sample rate {sr} != model {sd.sample_rate}; re-convert audio')

    t0 = time.time()
    last = [0]

    def cb(done, total):
        if done - last[0] >= max(1, total // 10) or done == total:
            print(f'  diar {done}/{total}  {time.time() - t0:.0f}s', flush=True)
            last[0] = done
        return 0

    result = sd.process(samples, callback=cb).sort_by_start_time()
    segs = [{'s': round(x.start, 2), 'e': round(x.end, 2), 'spk': int(x.speaker)} for x in result]
    segs = tidy(segs, a.min_speaker_seconds)
    out = a.out or os.path.join(wd, 'diarization.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(segs, f, ensure_ascii=False)

    from collections import Counter
    dur = Counter()
    for s in segs:
        dur[s['spk']] += s['e'] - s['s']
    summary = {f'S{k}': round(v, 1) for k, v in sorted(dur.items())}
    meta = {
        'num_speakers_hint': a.num_speakers,
        'threshold': a.threshold if n_spk < 0 else None,
        'speakers_found': len(dur),
        'seconds_per_speaker': summary,
        'segments': len(segs),
        'elapsed_s': round(time.time() - t0, 1),
        'segmentation': seg_path,
        'embedding': emb_path,
    }
    with open(os.path.join(wd, 'diarization_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f'wrote {out}  {len(segs)} segments, speakers {summary}  ({meta["elapsed_s"]}s)', flush=True)


if __name__ == '__main__':
    main()
