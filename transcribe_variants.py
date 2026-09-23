"""Run all preprocessing variants through the local /jobs API and collect transcripts.

Usage:
    1. Run this script. It cleans var/ and result/transcripts/, then prompts you.
    2. While prompted, start the backend in another shell:
         uv run paraspeakrs serve \
           --host 127.0.0.1 --port 8000 \
           --device coreml --senko-device coreml \
           --asr-backend sherpa \
           --sherpa-model-dir models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8
    3. Press Enter here. The script POSTs each wav, polls until done, writes
       result/transcripts/<variant>.txt.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).parent
OUT = ROOT / "result" / "transcripts"

VARIANTS: list[tuple[str, str]] = [
    ("baseline",                "test-audio/albert-gertjan-snippet-18-20.wav"),
    ("deepfilter_15db_pf",      "out/albert-gertjan-snippet-18-20_15db_pf.wav"),
    ("deepfilter_20db_pf",      "out/albert-gertjan-snippet-18-20_20db_pf.wav"),
    ("deepfilter_25db_pf",      "out/albert-gertjan-snippet-18-20_25db_pf.wav"),
    ("lavasr_bwe_only",         "out_lavasr/albert-gertjan-snippet-18-20_bwe_only.wav"),
    ("lavasr_denoise_only",     "out_lavasr/albert-gertjan-snippet-18-20_denoise_only.wav"),
    ("lavasr_enhance_denoise",  "out_lavasr/albert-gertjan-snippet-18-20_enhance_denoise.wav"),
    ("lavasr_enhance_only",     "out_lavasr/albert-gertjan-snippet-18-20_enhance_only.wav"),
    ("clearvoice_frcrn_se_16k", "out-clearvoice/albert-gertjan-snippet-18-20-clean.wav"),
]


def post_job(client: httpx.Client, wav_path: Path) -> str:
    with wav_path.open("rb") as fh:
        files = {"file": (wav_path.name, fh, "audio/wav")}
        r = client.post(f"{BASE}/jobs", files=files, timeout=60.0)
    r.raise_for_status()
    return r.json()["job_id"]


def wait_for(client: httpx.Client, job_id: str) -> None:
    last_step = None
    while True:
        r = client.get(f"{BASE}/jobs/{job_id}", timeout=30.0)
        r.raise_for_status()
        rec = r.json()
        status = rec["status"]
        step = rec.get("progress_step")
        detail = rec.get("progress_detail")
        pct = rec.get("progress_percent", 0)
        marker = (status, step, detail)
        if marker != last_step:
            print(f"  [{pct:>3}%] {status} {step or '-'}: {detail or ''}")
            last_step = marker
        if status == "completed":
            return
        if status == "failed":
            raise RuntimeError(f"job {job_id} failed: {rec.get('error')}")
        time.sleep(2.0)


def fetch_txt(client: httpx.Client, job_id: str) -> str:
    r = client.get(f"{BASE}/jobs/{job_id}/txt", timeout=30.0)
    r.raise_for_status()
    return r.text


def main() -> int:
    skip_clean = "--no-clean" in sys.argv
    if not skip_clean:
        shutil.rmtree(ROOT / "var", ignore_errors=True)
        shutil.rmtree(OUT, ignore_errors=True)
        print(f"Cleaned: var/, {OUT.relative_to(ROOT)}/")
    OUT.mkdir(parents=True, exist_ok=True)

    missing = [p for _, p in VARIANTS if not (ROOT / p).exists()]
    if missing:
        print("Missing input files:", *missing, sep="\n  ")
        return 1

    print("\nStart the backend now (see docstring for the exact command), then press Enter.")
    input()

    with httpx.Client() as client:
        try:
            client.get(f"{BASE}/jobs/does-not-exist", timeout=5.0)
        except httpx.HTTPError as exc:
            print(f"Backend not reachable at {BASE}: {exc}")
            return 1

        timings: list[tuple[str, float]] = []
        for name, rel_path in VARIANTS:
            wav = ROOT / rel_path
            print(f"\n=== {name} ({wav.name}) ===")
            t0 = time.monotonic()
            job_id = post_job(client, wav)
            print(f"  job_id={job_id}")
            wait_for(client, job_id)
            txt = fetch_txt(client, job_id)
            elapsed = time.monotonic() - t0
            out_path = OUT / f"{name}.txt"
            out_path.write_text(txt, encoding="utf-8")
            print(f"  wrote {out_path.relative_to(ROOT)} ({len(txt)} bytes) in {elapsed:.1f}s")
            timings.append((name, elapsed))

        print("\n--- Timings ---")
        for name, elapsed in timings:
            print(f"  {name:30s} {elapsed:6.1f}s")
        (OUT / "_timings.tsv").write_text(
            "variant\tseconds\n" + "\n".join(f"{n}\t{e:.2f}" for n, e in timings) + "\n",
            encoding="utf-8",
        )

    print(f"\nAll {len(VARIANTS)} variants written to {OUT.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
