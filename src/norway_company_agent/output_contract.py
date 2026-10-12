"""Adapter from the internal profile/evidence model to the documented output contract.

The authoritative contract is ``OUTPUT_CONTRACT.md`` (shipped with the starter kit),
which mirrors the official evaluation-harness contract:

    Exactly one terminal envelope per input organisation number. Envelopes must
    contain legal identity, claims, evidence references, availability states,
    source snapshots, refresh metadata and errors. Valid states are ``available``,
    ``not_available``, ``blocked``, ``not_applicable``, ``ambiguous`` and ``failed``.

This module never invents data. A field is only marked ``available`` when the
internal evidence record actually carries ``status == "available"``. Missing data,
blocked access and source errors are preserved as distinct availability states and
are never converted into successful claims or into zero.
"""

from __future__ import annotations

from typing import Any, Iterable

AVAILABILITY_STATES = (
    "available",
    "not_available",
    "blocked",
    "not_applicable",
    "ambiguous",
    "failed",
)

# Semantic mapping from the internal evidence vocabulary
# (``src/norway_company_agent/evidence.py``) to the documented availability states.
# This is a semantic mapping, not a string replacement:
#   available      -> available       source checked and returned data
#   not_found      -> not_available   source checked, no data present
#   not_applicable -> not_applicable  source does not apply to this company
#   blocked        -> blocked         policy / robots / access restriction
#   source_error   -> failed          source unreachable or returned an error
#   not_fetched    -> failed          no official "not checked" state exists;
#                                     recorded as an ambiguity (see release report)
STATUS_TO_AVAILABILITY = {
    "available": "available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "blocked": "blocked",
    "source_error": "failed",
    "not_fetched": "failed",
}

# Entity-level terminal status values used by the contract example ("completed").
TERMINAL_STATUS_VALUES = ("completed", "failed")

# Non-available states that must surface in the envelope's ``errors`` array.
_ERROR_STATES = {"blocked", "source_error", "not_fetched"}

# The core pipeline calls no paid third-party API (Brreg is public and keyless).
THIRD_PARTY_COST_USD = 0

# Website identity results that are not strong enough to publish as an asserted claim.
_AMBIGUOUS_IDENTITY = {"review", "related_or_uncertain"}

REQUIRED_TOP_LEVEL = ("organisation_number", "company", "run", "claims", "evidence", "changes", "errors", "operations")
REQUIRED_RUN_FIELDS = ("run_id", "started_at", "completed_at", "terminal_status")
REQUIRED_COMPANY_FIELDS = ("organisation_number", "name", "legal_form")
REQUIRED_OPERATIONS_FIELDS = ("requests", "third_party_cost_usd")


def _identity(profile: dict[str, Any]) -> dict[str, Any]:
    """Legal identity, taken from the resolved registry evidence only."""
    registry = (profile.get("evidence") or {}).get("registry_live")
    source = registry.get("value", {}) if isinstance(registry, dict) else {}
    if not isinstance(source, dict):
        source = {}
    return {
        "organisation_number": str(profile.get("organisation_number") or ""),
        "name": source.get("name") or profile.get("name"),
        "legal_form": source.get("legal_form") or profile.get("legal_form"),
    }


def availability_for(module: str, record: dict[str, Any]) -> str:
    """Map one internal evidence record to a documented availability state."""
    if not record:
        return "failed"
    mapped = STATUS_TO_AVAILABILITY.get(record.get("status"))
    if mapped is None:
        return "failed"
    if mapped == "available" and module == "website":
        assessment = (record.get("value") or {}).get("identity_assessment") or {}
        if assessment.get("status") in _AMBIGUOUS_IDENTITY:
            return "ambiguous"
    return mapped


def _confidence(module: str, record: dict[str, Any]) -> float | None:
    """Confidence only where the pipeline measured it; otherwise ``None``.

    We do not fabricate a confidence. The only measured identity signal is the
    website identity score, so it is the only value emitted here.
    """
    if module == "website" and record.get("status") == "available":
        assessment = (record.get("value") or {}).get("identity_assessment") or {}
        score = assessment.get("score")
        if isinstance(score, (int, float)):
            return round(float(score), 4)
    return None


def _evidence_entry(module: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"ev-{module}",
        "field": module,
        "source_url": record.get("source_url"),
        "source_class": record.get("source_class") or record.get("source_type"),
        "retrieved_at": record.get("retrieved_at"),
        "content_sha256": record.get("content_sha256"),
        # ``claim_span`` carries the registry row key when present, else the source
        # note. We do not fabricate literal source quotations we did not capture.
        "claim_span": record.get("source_row_key") or record.get("note"),
        "status": record.get("status"),
    }


def _errors(profile: dict[str, Any], modules: list[str]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    records = profile.get("evidence") or {}
    for module in modules:
        record = records.get(module)
        if not record:
            continue
        if record.get("status") in _ERROR_STATES:
            errors.append({
                "field": module,
                "state": availability_for(module, record),
                "source_url": record.get("source_url"),
                "note": record.get("note"),
                "content_sha256": record.get("content_sha256"),
            })
        value = record.get("value")
        for crawl_error in (value.get("crawl_errors") or []) if isinstance(value, dict) else []:
            errors.append({
                "field": module,
                "state": "failed",
                "source_url": crawl_error.get("url"),
                "note": crawl_error.get("error"),
                "content_sha256": None,
            })
    return errors


def _operations(profile: dict[str, Any]) -> dict[str, Any]:
    metrics = profile.get("run_metrics") or {}
    latencies = metrics.get("latencies_ms") or []
    return {
        "requests": int(metrics.get("requests") or 0),
        "bytes": int(metrics.get("bytes") or 0),
        "runtime_ms": int(sum(latencies)) if latencies else None,
        "third_party_cost_usd": THIRD_PARTY_COST_USD,
    }


def _terminal_status(profile: dict[str, Any], modules: list[str]) -> str:
    records = profile.get("evidence") or {}
    for module in modules:
        record = records.get(module)
        if not record or record.get("status") not in STATUS_TO_AVAILABILITY:
            return "failed"
    return "completed"


def contract_envelope(profile: dict[str, Any], *, run: dict[str, Any], modules: Iterable[str] | None = None) -> dict[str, Any]:
    """Build one documented-contract envelope from an internal profile."""
    org = str(profile.get("organisation_number") or "")
    if len(org) != 9 or not org.isdigit():
        raise ValueError(f"Invalid Norwegian organisation number: {profile.get('organisation_number')!r}")
    records = profile.get("evidence") or {}
    ordered_modules = [module for module in (list(modules) if modules is not None else list(records)) if module in records]
    claims = []
    evidence_entries = []
    for module in ordered_modules:
        record = records[module]
        evidence_entries.append(_evidence_entry(module, record))
        claims.append({
            "field": module,
            "value": record.get("value"),
            "availability": availability_for(module, record),
            "confidence": _confidence(module, record),
            "evidence_ids": [f"ev-{module}"],
        })
    return {
        "organisation_number": org,
        "company": _identity(profile),
        "run": {
            "run_id": run.get("run_id"),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "terminal_status": _terminal_status(profile, ordered_modules),
        },
        "claims": claims,
        "evidence": evidence_entries,
        "changes": [],
        "refresh": {
            "based_on_snapshot": profile.get("refresh_based_on"),
            "change_count": 0,
            "idempotent": True,
        },
        "errors": _errors(profile, ordered_modules),
        "operations": _operations(profile),
    }


def build_contract_envelopes(profiles: Iterable[dict[str, Any]], run: dict[str, Any], modules: Iterable[str] | None = None) -> list[dict[str, Any]]:
    ordered = list(modules) if modules is not None else None
    return [contract_envelope(profile, run=run, modules=ordered) for profile in profiles]


def validate_contract_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Structural validation of a single envelope against the documented contract."""
    problems: list[str] = []
    for field in REQUIRED_TOP_LEVEL:
        if field not in envelope:
            problems.append(f"missing top-level field: {field}")
    org = str(envelope.get("organisation_number") or "")
    if len(org) != 9 or not org.isdigit():
        problems.append("organisation_number must be 9 digits")
    run = envelope.get("run") or {}
    for field in REQUIRED_RUN_FIELDS:
        if field not in run:
            problems.append(f"missing run field: {field}")
    if run.get("terminal_status") not in TERMINAL_STATUS_VALUES:
        problems.append(f"invalid terminal_status: {run.get('terminal_status')!r}")
    company = envelope.get("company") or {}
    for field in REQUIRED_COMPANY_FIELDS:
        if field not in company:
            problems.append(f"missing company field: {field}")
    operations = envelope.get("operations") or {}
    for field in REQUIRED_OPERATIONS_FIELDS:
        if field not in operations:
            problems.append(f"missing operations field: {field}")
    if not isinstance(envelope.get("changes"), list):
        problems.append("changes must be a list")
    if not isinstance(envelope.get("errors"), list):
        problems.append("errors must be a list")
    evidence = envelope.get("evidence") or []
    ids = [entry.get("id") for entry in evidence]
    if len(ids) != len(set(ids)):
        problems.append("duplicate evidence ids")
    known_ids = set(ids)
    for claim in envelope.get("claims") or []:
        if claim.get("availability") not in AVAILABILITY_STATES:
            problems.append(f"invalid availability for field {claim.get('field')!r}: {claim.get('availability')!r}")
        confidence = claim.get("confidence")
        if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
            problems.append(f"confidence out of range for field {claim.get('field')!r}")
        for evidence_id in claim.get("evidence_ids") or []:
            if evidence_id not in known_ids:
                problems.append(f"unresolved evidence id {evidence_id!r} for field {claim.get('field')!r}")
    return {"passed": not problems, "problems": problems}


def validate_contract_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    """Batch validation: one terminal envelope per input, no silent drops."""
    per_envelope = [validate_contract_envelope(item) for item in envelopes]
    orgs = [item.get("organisation_number") for item in envelopes]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_envelopes_valid": all(item["passed"] for item in per_envelope),
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "problems": [problem for item in per_envelope for problem in item["problems"]],
    }
