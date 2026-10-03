#!/usr/bin/env python3
"""Benchmark faster-whisper worker/thread layouts without changing production ASR.

The audio is converted and chunked before the timer starts. Timed work includes
one model load per worker plus transcription of all chunks. "static" reproduces
the production round-robin assignment (chunk_index % workers). "dynamic" uses a
shared queue so the next free worker takes the next chunk.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transcribe import plan_chunks, read_wav, to_wav  # noqa: E402


def _transcribe_one(model, chunk_path, start, args):
    t0 = time.perf_counter()
    segs, _ = model.transcribe(
        chunk_path,
        language=args.lang,
        beam_size=args.beam,
        word_timestamps=True,
        no_speech_threshold=None,
        log_prob_threshold=None,
        initial_prompt=args.prompt or None,
    )
    rows = []
    for s in segs:
        rows.append({
            "s": round(s.start + start, 3),
            "e": round(s.end + start, 3),
            "text": s.text.strip(),
            "words": [
                [round(w.start + start, 3), round(w.end + start, 3), w.word]
                for w in (s.words or [])
            ],
        })
    return rows, time.perf_counter() - t0


def _load_model(args):
    from faster_whisper import WhisperModel

    t0 = time.perf_counter()
    model = WhisperModel(
        args.model,
        device="cpu",
        compute_type=args.compute_type,
        cpu_threads=args.threads,
        download_root=args.model_dir,
    )
    return model, time.perf_counter() - t0


def _write_chunk(cdir, k, rows):
    out = os.path.join(cdir, f"k{k:02d}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


def static_worker(part, nparts, args, chunks, cdir, results):
    model, load_sec = _load_model(args)
    done = []
    work_sec = 0.0
    for k, (a, _b) in enumerate(chunks):
        if k % nparts != part:
            continue
        rows, sec = _transcribe_one(model, os.path.join(cdir, f"k{k:02d}.wav"), a, args)
        _write_chunk(cdir, k, rows)
        done.append({"chunk": k, "seconds": round(sec, 3)})
        work_sec += sec
        print(f"[worker {part}] chunk {k:02d} {sec:.2f}s", flush=True)
    results.put({
        "worker": part,
        "model_load_seconds": round(load_sec, 3),
        "work_seconds": round(work_sec, 3),
        "chunks": done,
    })


def dynamic_worker(part, args, chunks, cdir, tasks, results):
    model, load_sec = _load_model(args)
    done = []
    work_sec = 0.0
    while True:
        k = tasks.get()
        if k is None:
            break
        a, _b = chunks[k]
        rows, sec = _transcribe_one(model, os.path.join(cdir, f"k{k:02d}.wav"), a, args)
        _write_chunk(cdir, k, rows)
        done.append({"chunk": k, "seconds": round(sec, 3)})
        work_sec += sec
        print(f"[worker {part}] chunk {k:02d} {sec:.2f}s", flush=True)
    results.put({
        "worker": part,
        "model_load_seconds": round(load_sec, 3),
        "work_seconds": round(work_sec, 3),
        "chunks": done,
    })


def prepare_chunks(audio, workdir, chunk_sec):
    cdir = os.path.join(workdir, "chunks")
    os.makedirs(cdir, exist_ok=True)
    wav = os.path.join(workdir, "audio16k.wav")
    to_wav(audio, wav)
    chunks = plan_chunks(read_wav(wav), target=chunk_sec)
    for k, (a, b) in enumerate(chunks):
        path = os.path.join(cdir, f"k{k:02d}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(a), "-t", str(b - a), "-i", wav, path],
            check=True,
        )
    return cdir, chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--source", default="custom")
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--model-dir")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--chunk", type=float, default=120.0)
    ap.add_argument("--jobs", type=int, required=True)
    ap.add_argument("--threads", type=int, required=True)
    ap.add_argument("--schedule", choices=["static", "dynamic"], required=True)
    ap.add_argument("--compute-type", default="float32")
    ap.add_argument("--beam", type=int, default=5)
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    cdir, chunks = prepare_chunks(args.audio, args.workdir, args.chunk)
    cpu = os.cpu_count() or 1
    # One float32 large-v3-turbo copy per worker. Five of them OOM a 16 GB runner
    # once the next chunk allocates while earlier workers are still decoding.
    worker_cap = cpu if args.compute_type == "float32" else max(cpu, args.jobs)
    workers = max(1, min(args.jobs, len(chunks), worker_cap))
    if workers < args.jobs:
        print(
            f"capping workers {args.jobs} -> {workers} "
            f"(compute_type={args.compute_type}, cpu={cpu})",
            flush=True,
        )
    total_audio_sec = chunks[-1][1] if chunks else 0.0

    print(
        f"benchmark {args.label}: audio={total_audio_sec:.1f}s chunks={len(chunks)} "
        f"workers={workers} threads/worker={args.threads} schedule={args.schedule}",
        flush=True,
    )

    ctx = mp.get_context("spawn")
    results = ctx.Queue()
    procs = []
    t0 = time.perf_counter()

    if workers == 1:
        static_worker(0, 1, args, chunks, cdir, results)
    elif args.schedule == "static":
        procs = [
            ctx.Process(target=static_worker, args=(p, workers, args, chunks, cdir, results))
            for p in range(workers)
        ]
        for p in procs:
            p.start()
    else:
        tasks = ctx.Queue()
        for k in range(len(chunks)):
            tasks.put(k)
        for _ in range(workers):
            tasks.put(None)
        procs = [
            ctx.Process(target=dynamic_worker, args=(p, args, chunks, cdir, tasks, results))
            for p in range(workers)
        ]
        for p in procs:
            p.start()

    if procs:
        for p in procs:
            p.join()
        if any(p.exitcode for p in procs):
            raise SystemExit("a benchmark worker failed")

    elapsed = time.perf_counter() - t0
    worker_rows = [results.get() for _ in range(workers)]
    worker_rows.sort(key=lambda x: x["worker"])
    chunk_rows = sorted(
        [dict(x, worker=w["worker"]) for w in worker_rows for x in w["chunks"]],
        key=lambda x: x["chunk"],
    )

    result = {
        "label": args.label,
        "source": args.source,
        "model": args.model,
        "compute_type": args.compute_type,
        "schedule": args.schedule,
        "jobs": workers,
        "threads_per_job": args.threads,
        "total_compute_threads": workers * args.threads,
        "cpu_count": os.cpu_count(),
        "audio_seconds": round(total_audio_sec, 3),
        "chunks": len(chunks),
        "elapsed_seconds": round(elapsed, 3),
        "realtime_factor": round(elapsed / total_audio_sec, 4) if total_audio_sec else None,
        "workers": worker_rows,
        "chunk_timings": chunk_rows,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.result)), exist_ok=True)
    with open(args.result, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(
        f"RESULT {args.label}: {elapsed:.2f}s, RTF={result['realtime_factor']}, "
        f"worker_work={[w['work_seconds'] for w in worker_rows]}",
        flush=True,
    )


if __name__ == "__main__":
    main()
