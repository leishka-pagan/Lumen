"""Import validated live AI captures into the committed seed ``data/live_capture_seed.json``.

This runs ONCE, against the external capture bundles produced by the guarded capture
runners. It validates every bundle and distils it into a committed, self-contained seed
holding only NORMALIZED capture records and claims.

    py scripts/import_live_captures.py \
        --capture <MANIFEST_JSON> <OUTPUT_DIR> \
        --capture <MANIFEST_JSON> <OUTPUT_DIR> \
        [--out data/live_capture_seed.json] [--dry-run]

The seed deliberately carries NO transport detail: no API key, prompt, raw provider
response, token usage, manifest, hash, or external filesystem path. It records only the
alert id, capture timestamp, capture run id, the fixed model, and the normalized claims —
exactly what ``scripts/generate_data.py`` needs to rebuild ai_outputs.csv and
case_lifecycle.csv deterministically, with no network and no API call.

A bundle is refused (and nothing is written) unless every alert is ``succeeded``, every
output hash matches its manifest, every output names the fixed capture model, and every
capture carries 1..3 well-formed normalized claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import verifier  # noqa: E402
from src.schema import AiOutput  # noqa: E402

CAPTURE_MODEL = "claude-haiku-4-5-20251001"
SEED_SCHEMA_VERSION = 1
DEFAULT_SEED_PATH = _ROOT / "data" / "live_capture_seed.json"
MIN_CLAIMS, MAX_CLAIMS = 1, 3

# The exact per-claim keys the seed stores. Anything else a capture file happens to carry
# (token usage in particular) is deliberately dropped.
CLAIM_KEYS = ("output_id", "alert_id", "claim_id", "claim_type",
              "asserted_value", "evidence_refs", "generated_at")


class CaptureImportError(Exception):
    """Raised when a capture bundle is not fit to import. Fail-closed: nothing is written."""


@dataclass(frozen=True)
class CaptureSource:
    manifest_path: Path
    output_dir: Path


# ── validation ───────────────────────────────────────────────────────────────
def validate_bundle(source: CaptureSource) -> dict[str, dict]:
    """Validate one capture bundle; return {alert_id: capture_document}."""
    try:
        manifest = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CaptureImportError(f"manifest unreadable: {source.manifest_path.name}") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("alerts"), dict):
        raise CaptureImportError(f"manifest malformed: {source.manifest_path.name}")
    if manifest.get("model") != CAPTURE_MODEL:
        raise CaptureImportError(f"manifest model is not {CAPTURE_MODEL}")

    captured: dict[str, dict] = {}
    for alert_id, entry in sorted(manifest["alerts"].items()):
        if entry.get("status") != "succeeded":
            raise CaptureImportError(f"{alert_id}: capture status is {entry.get('status')!r}, not succeeded")
        if entry.get("failure_code") is not None:
            raise CaptureImportError(f"{alert_id}: capture carries a failure code")
        filename = entry.get("output_filename")
        if not filename:
            raise CaptureImportError(f"{alert_id}: manifest has no output_filename")
        try:
            text = (source.output_dir / str(filename)).read_text(encoding="utf-8")
        except OSError:
            raise CaptureImportError(f"{alert_id}: capture output is missing") from None
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != entry.get("output_sha256"):
            raise CaptureImportError(f"{alert_id}: capture output hash mismatch")
        try:
            doc = json.loads(text)
        except ValueError:
            raise CaptureImportError(f"{alert_id}: capture output is not valid JSON") from None
        _validate_capture_document(alert_id, doc)
        captured[alert_id] = doc
    return captured


def _validate_capture_document(alert_id: str, doc: Any) -> None:
    if not isinstance(doc, dict):
        raise CaptureImportError(f"{alert_id}: capture document is not an object")
    if doc.get("alert_id") != alert_id:
        raise CaptureImportError(f"{alert_id}: capture alert_id mismatch")
    if doc.get("model") != CAPTURE_MODEL:
        raise CaptureImportError(f"{alert_id}: capture model is {doc.get('model')!r}")
    for field in ("processing_run_id", "captured_at"):
        if not str(doc.get(field) or "").strip():
            raise CaptureImportError(f"{alert_id}: capture has no {field}")
    claims = doc.get("claims")
    if not isinstance(claims, list) or not (MIN_CLAIMS <= len(claims) <= MAX_CLAIMS):
        n = len(claims) if isinstance(claims, list) else "?"
        raise CaptureImportError(f"{alert_id}: capture has {n} claims; expected {MIN_CLAIMS}..{MAX_CLAIMS}")
    for claim in claims:
        if not isinstance(claim, dict):
            raise CaptureImportError(f"{alert_id}: a claim is not an object")
        for key in CLAIM_KEYS:
            if key not in claim:
                raise CaptureImportError(f"{alert_id}: claim is missing {key}")
        if claim["alert_id"] != alert_id:
            raise CaptureImportError(f"{alert_id}: a claim carries a different alert_id")
        if claim["claim_type"] not in verifier.KNOWN_CLAIM_TYPES:
            raise CaptureImportError(f"{alert_id}: unknown claim_type {claim['claim_type']!r}")
        refs = claim["evidence_refs"]
        if not isinstance(refs, list) or any(not isinstance(r, str) or not r.strip() for r in refs):
            raise CaptureImportError(f"{alert_id}: malformed evidence_refs")
        if claim["claim_type"] in verifier.REQUIRES_EVIDENCE_REFS and not refs:
            raise CaptureImportError(f"{alert_id}: claim requires evidence_refs but has none")


def load_captures(sources: Sequence[CaptureSource]) -> dict[str, dict]:
    """Validate every bundle and merge, refusing a duplicated alert id."""
    merged: dict[str, dict] = {}
    for source in sources:
        for alert_id, doc in validate_bundle(source).items():
            if alert_id in merged:
                raise CaptureImportError(f"{alert_id}: duplicated across capture bundles")
            merged[alert_id] = doc
    if not merged:
        raise CaptureImportError("no captures found")
    return merged


# ── seed construction ────────────────────────────────────────────────────────
def build_seed(captures: Mapping[str, dict]) -> dict:
    """Distil validated captures into the committed seed document.

    Copies ONLY the normalized fields; every claim is re-validated against the AiOutput
    schema first, and output/claim ids must be globally unique.
    """
    seen_output_ids: set[str] = set()
    seen_claim_ids: set[str] = set()
    records: list[dict] = []

    for alert_id in sorted(captures):
        doc = captures[alert_id]
        claims: list[dict] = []
        for claim in doc["claims"]:
            try:
                AiOutput(
                    output_id=claim["output_id"], alert_id=claim["alert_id"],
                    claim_id=claim["claim_id"], claim_type=claim["claim_type"],
                    asserted_value=claim["asserted_value"],
                    evidence_refs=list(claim["evidence_refs"]),
                    generated_at=claim["generated_at"],
                )
            except Exception as exc:
                raise CaptureImportError(
                    f"{alert_id}: claim fails the AiOutput schema ({type(exc).__name__})"
                ) from None
            if claim["output_id"] in seen_output_ids:
                raise CaptureImportError(f"duplicate output_id {claim['output_id']}")
            if claim["claim_id"] in seen_claim_ids:
                raise CaptureImportError(f"duplicate claim_id {claim['claim_id']}")
            seen_output_ids.add(claim["output_id"])
            seen_claim_ids.add(claim["claim_id"])
            claims.append({
                "output_id": claim["output_id"],
                "alert_id": claim["alert_id"],
                "claim_id": claim["claim_id"],
                "claim_type": claim["claim_type"],
                "asserted_value": claim["asserted_value"],
                "evidence_refs": list(claim["evidence_refs"]),
                "generated_at": claim["generated_at"],
            })
        records.append({
            "alert_id": alert_id,
            "captured_at": str(doc["captured_at"]).strip(),
            "processing_run_id": str(doc["processing_run_id"]).strip(),
            "claims": claims,
        })

    return {
        "schema_version": SEED_SCHEMA_VERSION,
        "model": CAPTURE_MODEL,
        "captures": records,
    }


def seed_text(seed: Mapping[str, Any]) -> str:
    """Canonical serialization — stable key order so regeneration is byte-reproducible."""
    return json.dumps(seed, indent=2, sort_keys=True) + "\n"


def write_seed(seed: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(seed_text(seed), encoding="utf-8", newline="")


# ── CLI ──────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="import_live_captures",
        description="Validate external live-capture bundles and write the committed "
                    "live_capture_seed.json. Makes no API request and opens no network.",
    )
    p.add_argument("--capture", nargs=2, action="append", metavar=("MANIFEST", "OUTPUT_DIR"),
                   required=True, help="A capture bundle: manifest JSON and output directory. Repeatable.")
    p.add_argument("--out", default=str(DEFAULT_SEED_PATH), help="Seed destination.")
    p.add_argument("--dry-run", action="store_true", help="Validate and summarize without writing.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        captures = load_captures([CaptureSource(Path(m), Path(o)) for m, o in args.capture])
        seed = build_seed(captures)
    except CaptureImportError as exc:
        print(f"import refused: {exc}", file=sys.stderr)
        return 1
    n_claims = sum(len(r["claims"]) for r in seed["captures"])
    if not args.dry_run:
        write_seed(seed, Path(args.out))
    label = "DRY RUN (nothing written)" if args.dry_run else f"WROTE {args.out}"
    print(f"{label}: {len(seed['captures'])} capture records, {n_claims} claims, model {seed['model']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
