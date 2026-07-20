"""Tests for the live-capture importer and the committed seed it produces.

Two layers:

1. IMPORTER tests drive the validator against SYNTHETIC capture bundles written into
   pytest ``tmp_path``. They never read the external capture directories and never write
   into data/.
2. SEED tests assert the committed data/live_capture_seed.json is the canonical, fully
   normalized, transport-free source that scripts/generate_data.py consumes.

No test makes an API request or opens a network connection.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

from src.schema import AiOutput  # noqa: E402
from src.verifier import KNOWN_CLAIM_TYPES  # noqa: E402

SCRIPT = ROOT / "scripts" / "import_live_captures.py"
SEED_PATH = DATA / "live_capture_seed.json"
MODEL = "claude-haiku-4-5-20251001"
ALL_ALERTS = [f"ALERT{n:03d}" for n in range(1, 40)]


def _load_script():
    spec = importlib.util.spec_from_file_location("import_live_captures", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


imp = _load_script()
CaptureImportError = imp.CaptureImportError


# ── synthetic capture bundles (tmp_path only) ────────────────────────────────
def _claim(alert_id, n=0, **over):
    claim = {
        "alert_id": alert_id,
        "asserted_value": "true",
        "claim_id": f"CLM-{alert_id[-3:]}-{n}",
        "claim_type": "prior_sar_history",
        "evidence_refs": ["prior_cases.customer_id=CUST0001"],
        "generated_at": "2026-07-20T03:40:14.079719+00:00",
        "output_id": f"OUT-{alert_id[-3:]}-{n}",
    }
    claim.update(over)
    return claim


def _doc(alert_id, claims=None, model=MODEL, run_id="LIVE-CAPTURE-V1", **over):
    doc = {
        "alert_id": alert_id,
        "captured_at": "2026-07-20T03:40:14.079756+00:00",
        "claims": claims if claims is not None else [_claim(alert_id)],
        "input_tokens": 1852,          # transport detail: must NOT reach the seed
        "output_tokens": 241,
        "model": model,
        "processing_run_id": run_id,
    }
    doc.update(over)
    return doc


def _bundle(tmp_path, name, docs, *, corrupt_hash=None, status_override=None):
    out = tmp_path / name / "out"
    out.mkdir(parents=True, exist_ok=True)
    entries = {}
    for alert_id, doc in docs.items():
        text = json.dumps(doc, sort_keys=True, indent=2)
        (out / f"{alert_id}.capture.json").write_text(text, encoding="utf-8", newline="")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if corrupt_hash == alert_id:
            digest = "0" * 64
        entries[alert_id] = {
            "status": (status_override or {}).get(alert_id, "succeeded"),
            "output_filename": f"{alert_id}.capture.json",
            "output_sha256": digest, "failure_code": None,
            "request_chars": 4378, "estimated_input_tokens": 1251,
            "actual_input_tokens": 1852, "actual_output_tokens": 241,
        }
    manifest = tmp_path / name / "manifest.json"
    manifest.write_text(json.dumps(
        {"schema_version": 1, "model": MODEL, "alerts": entries}, sort_keys=True, indent=2),
        encoding="utf-8", newline="")
    return imp.CaptureSource(manifest, out)


def _sources(tmp_path, docs, **kw):
    return [_bundle(tmp_path, "b", docs, **kw)]


# ── fail-closed validation ───────────────────────────────────────────────────
def test_valid_bundle_builds_a_seed(tmp_path):
    seed = imp.build_seed(imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001")})))
    assert seed["model"] == MODEL
    assert len(seed["captures"]) == 1
    assert seed["captures"][0]["alert_id"] == "ALERT001"


def test_hash_mismatch_is_refused(tmp_path):
    with pytest.raises(CaptureImportError, match="hash mismatch"):
        imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001")},
                                   corrupt_hash="ALERT001"))


@pytest.mark.parametrize("status", ["pending", "failed", "planned"])
def test_non_succeeded_status_is_refused(tmp_path, status):
    with pytest.raises(CaptureImportError, match="not succeeded"):
        imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001")},
                                   status_override={"ALERT001": status}))


def test_wrong_model_is_refused(tmp_path):
    with pytest.raises(CaptureImportError, match="model"):
        imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001", model="claude-3-opus")}))


@pytest.mark.parametrize("n", [0, 4])
def test_claim_count_outside_1_to_3_is_refused(tmp_path, n):
    claims = [_claim("ALERT001", i) for i in range(n)]
    with pytest.raises(CaptureImportError, match="claims"):
        imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001", claims=claims)}))


def test_unknown_claim_type_is_refused(tmp_path):
    bad = [_claim("ALERT001", claim_type="NOT_A_TYPE")]
    with pytest.raises(CaptureImportError, match="unknown claim_type"):
        imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001", claims=bad)}))


@pytest.mark.parametrize("refs", [["ok", ""], ["ok", 123], "notalist"])
def test_malformed_evidence_refs_is_refused(tmp_path, refs):
    bad = [_claim("ALERT001", evidence_refs=refs)]
    with pytest.raises(CaptureImportError, match="evidence_refs"):
        imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001", claims=bad)}))


def test_alert_duplicated_across_bundles_is_refused(tmp_path):
    a = _bundle(tmp_path, "a", {"ALERT001": _doc("ALERT001")})
    b = _bundle(tmp_path, "b", {"ALERT001": _doc("ALERT001")})
    with pytest.raises(CaptureImportError, match="duplicated"):
        imp.load_captures([a, b])


def test_duplicate_output_id_is_refused(tmp_path):
    dup = [_claim("ALERT001", 0), _claim("ALERT001", 1, output_id="OUT-001-0")]
    with pytest.raises(CaptureImportError, match="duplicate output_id"):
        imp.build_seed(imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001", claims=dup)})))


def test_seed_drops_transport_detail(tmp_path):
    """Token usage present on the capture document must never reach the seed."""
    seed = imp.build_seed(imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001")})))
    record = seed["captures"][0]
    assert set(record) == {"alert_id", "captured_at", "processing_run_id", "claims"}
    assert set(record["claims"][0]) == set(imp.CLAIM_KEYS)
    text = imp.seed_text(seed)
    for banned in ("input_tokens", "output_tokens", "sha256", "manifest", "capture.json"):
        assert banned not in text


def test_import_opens_no_network(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    seed = imp.build_seed(imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001")})))
    assert len(seed["captures"]) == 1


def test_seed_serialization_is_stable(tmp_path):
    captures = imp.load_captures(_sources(tmp_path, {"ALERT001": _doc("ALERT001")}))
    assert imp.seed_text(imp.build_seed(captures)) == imp.seed_text(imp.build_seed(captures))


# ══ the COMMITTED seed ═══════════════════════════════════════════════════════
def _seed() -> dict:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


def test_committed_seed_covers_all_39_alerts_and_93_claims():
    seed = _seed()
    assert seed["model"] == MODEL
    records = seed["captures"]
    assert len(records) == 39
    assert sorted(r["alert_id"] for r in records) == ALL_ALERTS
    assert sum(len(r["claims"]) for r in records) == 93
    for r in records:
        assert 1 <= len(r["claims"]) <= 3, r["alert_id"]
        assert r["captured_at"] and r["processing_run_id"]


def test_committed_seed_ids_are_unique_and_schema_valid():
    output_ids, claim_ids = set(), set()
    for record in _seed()["captures"]:
        for claim in record["claims"]:
            assert claim["alert_id"] == record["alert_id"]
            assert claim["claim_type"] in KNOWN_CLAIM_TYPES
            AiOutput(
                output_id=claim["output_id"], alert_id=claim["alert_id"],
                claim_id=claim["claim_id"], claim_type=claim["claim_type"],
                asserted_value=claim["asserted_value"],
                evidence_refs=list(claim["evidence_refs"]),
                generated_at=claim["generated_at"],
            )
            assert claim["output_id"] not in output_ids
            assert claim["claim_id"] not in claim_ids
            output_ids.add(claim["output_id"])
            claim_ids.add(claim["claim_id"])
    assert len(output_ids) == len(claim_ids) == 93


def test_committed_seed_carries_no_transport_detail():
    raw = SEED_PATH.read_text(encoding="utf-8")
    for banned in ("input_tokens", "output_tokens", "sha256", "manifest", "sk-ant",
                   "ANTHROPIC_API_KEY", "capture.json", "C:\\", "/Users/"):
        assert banned not in raw, f"{banned} leaked into the committed seed"
    for record in _seed()["captures"]:
        assert set(record) == {"alert_id", "captured_at", "processing_run_id", "claims"}
        for claim in record["claims"]:
            assert set(claim) == set(imp.CLAIM_KEYS)


def test_committed_seed_matches_committed_ai_outputs():
    """The seed is the source: every committed ai_outputs row comes from it, verbatim."""
    import pandas as pd

    ai = pd.read_csv(DATA / "ai_outputs.csv", keep_default_na=False, dtype=str)
    seeded = {c["output_id"]: (c, r) for r in _seed()["captures"] for c in r["claims"]}
    assert len(ai) == len(seeded) == 93
    for row in ai.to_dict("records"):
        claim, record = seeded[row["output_id"]]
        assert row["alert_id"] == claim["alert_id"]
        assert row["claim_id"] == claim["claim_id"]
        assert row["claim_type"] == claim["claim_type"]
        assert row["asserted_value"] == claim["asserted_value"]
        assert json.loads(row["evidence_refs"]) == claim["evidence_refs"]
        assert row["generated_at"] == claim["generated_at"]
        assert row["draft_source"] == "captured_live"
        assert row["model_id"] == MODEL
        assert row["processing_run_id"] == record["processing_run_id"]
