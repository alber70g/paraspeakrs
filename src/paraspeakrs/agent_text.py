"""Short plain-text summaries of the agent commands' reports, printed on stdout.

An agent reads stdout into its context, so the summary says only what it acts
on: where the output is, which samples need a name, and what to run next. The
first lines of each speaker are in NEXT_STEPS.md; the full report is behind
``--json``.
"""

from __future__ import annotations


def staged(report: dict) -> str:
    """Summary of ``agent transcribe`` or ``agent diarize``."""
    output = report.get("transcript") or report["diarization"]
    reused = " (reused the earlier job for identical audio)" if report["reused"] else ""
    speakers = report["speakers"]
    width = max((len(s["file"]) for s in speakers), default=0)
    lines = [
        f"Wrote {output}{reused}",
        f"Speaker samples in {report['out_dir']}:",
        *(f"  {s['file']:<{width}}  {s['talk_seconds']:.0f} s" for s in speakers),
        f"Next: {report['next']}",
    ]
    return "\n".join(lines) + "\n"


def applied(result: dict) -> str:
    """Summary of ``agent apply``: one row per speaker with what happened to it."""
    output = result.get("transcript") or result["diarization"]
    if result["dry_run"]:
        head = f"Dry run, nothing changed. Apply would rewrite {output} as:"
    else:
        count = f" ({result['lines']} lines)" if "lines" in result else ""
        head = f"Rewrote {output}{count}:"
    speakers = result["speakers"]
    width = max((len(s["speaker"]) for s in speakers), default=0)
    rows = [f"  {s['speaker']:<{width}}  {s['action']:<10}  {s['name'] or ''}".rstrip() for s in speakers]
    return "\n".join([head, *rows]) + "\n"
