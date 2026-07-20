"""CLI for the guarded, resumable, one-time Anthropic AI-draft capture runner.

DEFAULT IS DRY-RUN — no API key is read, no network is opened, and nothing is written:

    py scripts/capture_ai_drafts.py

A live run requires EVERY gate and both paths absolute and OUTSIDE the repository:

    py scripts/capture_ai_drafts.py --live --confirm CAPTURE-7-AUTHORIZED \
        --manifest ABSOLUTE_PATH --output-dir ABSOLUTE_DIRECTORY

Live mode additionally requires the environment LUMEN_CAPTURE_LIVE_AI=1 and a present
ANTHROPIC_API_KEY. Any missing gate fails before an Anthropic client is constructed or
a network connection is opened. This script never prints or persists the API key.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import live_capture  # noqa: E402
from src.capture_guardrails import REQUIRED_CONFIRMATION_PHRASE  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capture_ai_drafts",
        description="Guarded one-time Anthropic AI-draft capture for ALERT001..ALERT007 "
                    "(default: dry-run; makes no live API request).",
    )
    p.add_argument("--live", action="store_true",
                   help="Perform the live capture. Default (omitted) is a dry-run.")
    p.add_argument("--confirm", default="",
                   help=f"Confirmation phrase; must be exactly {REQUIRED_CONFIRMATION_PHRASE}.")
    p.add_argument("--manifest", default="",
                   help="Absolute manifest path OUTSIDE the git repository (live only).")
    p.add_argument("--output-dir", dest="output_dir", default="",
                   help="Absolute output directory OUTSIDE the git repository (live only).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.live:
        # Dry-run: plan the seven requests; exit 0 only if all fit the guardrails.
        return 0 if live_capture.dry_run(print_fn=print) else 2

    try:
        summary = live_capture.run_capture(
            env=os.environ,
            confirmation_phrase=args.confirm,
            manifest_path=args.manifest,
            output_dir=args.output_dir,
        )
    except live_capture.CaptureError as exc:
        # Non-sensitive refusal message (never the key, prompt, or provider text).
        print(f"capture refused: {exc}", file=sys.stderr)
        return 1
    print(f"capture complete: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
