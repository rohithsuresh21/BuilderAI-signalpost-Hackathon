from __future__ import annotations

import csv
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.http import FetchResult  # noqa: E402
from norway_company_agent.output_contract import (  # noqa: E402
    AVAILABILITY_STATES,
    REQUIRED_TOP_LEVEL,
    availability_for,
    build_contract_envelopes,
    contract_envelope,
    validate_contract_envelope,
    validate_contract_envelopes,
)
from norway_company_agent.batch import terminal_envelope, validate_envelopes  # noqa: E402

RUN = {"run_id": "unit-test", "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:01:00Z"}


def make_profile(records, *, org="923609016", name="Example AS", legal_form="AS", run_metrics=None):
    return {
        "organisation_number": org,
        "name": name,
        "legal_form": legal_form,
        "evidence": records,
        "run_metrics": run_metrics or {},
    }


class AvailabilityMappingTests(unittest.TestCase):
    def test_available_maps_to_available(self):
        record = evidence("registry_live", "available", "official_registry_live", "https://data.brreg.no/x")
        self.assertEqual(availability_for("registry_live", record), "available")

    def test_not_found_maps_to_not_available_never_zero(self):
        record = evidence("financials", "not_found", "official_annual_accounts", "https://data.brreg.no/x")
        self.assertEqual(availability_for("financials", record), "not_available")
        envelope = contract_envelope(make_profile({"financials": record}), run=RUN, modules=["financials"])
        claim = envelope["claims"][0]
        self.assertEqual(claim["availability"], "not_available")
        self.assertIsNone(claim["value"])
        self.assertNotEqual(claim["value"], 0)

    def test_not_applicable_maps_to_not_applicable(self):
        record = evidence("locations", "not_applicable", "official_subunits", "https://data.brreg.no/x")
        self.assertEqual(availability_for("locations", record), "not_applicable")

    def test_blocked_maps_to_blocked(self):
        record = evidence("group", "blocked", "official_group_structure", "https://data.brreg.no/x", note="robots.txt denied")
        self.assertEqual(availability_for("group", record), "blocked")

    def test_source_error_maps_to_failed(self):
        record = evidence("roles", "source_error", "official_roles", "https://data.brreg.no/x", note="HTTP 500")
        self.assertEqual(availability_for("roles", record), "failed")

    def test_not_fetched_is_failed_not_available(self):
        record = evidence("misc", "not_fetched", "official", "https://data.brreg.no/x")
        self.assertEqual(availability_for("misc", record), "failed")

    def test_ambiguous_website_identity_does_not_become_available(self):
        record = evidence(
            "website", "available", "registry_linked_company_website", "https://example.no/",
            value={"final_url": "https://example.no/", "identity_assessment": {"status": "review", "score": 0.65}},
        )
        self.assertEqual(availability_for("website", record), "ambiguous")
        envelope = contract_envelope(make_profile({"website": record}), run=RUN, modules=["website"])
        self.assertEqual(envelope["claims"][0]["availability"], "ambiguous")
        self.assertIn(envelope["claims"][0]["availability"], AVAILABILITY_STATES)

    def test_exact_website_identity_is_available_with_measured_confidence(self):
        record = evidence(
            "website", "available", "registry_linked_company_website", "https://example.no/",
            value={"final_url": "https://example.no/", "identity_assessment": {"status": "exact", "score": 0.95}},
        )
        envelope = contract_envelope(make_profile({"website": record}), run=RUN, modules=["website"])
        self.assertEqual(envelope["claims"][0]["availability"], "available")
        self.assertEqual(envelope["claims"][0]["confidence"], 0.95)

    def test_every_internal_status_produces_a_document_state(self):
        for status in ("available", "not_found", "not_applicable", "not_fetched", "source_error", "blocked"):
            record = evidence("misc", status, "official", "https://data.brreg.no/x", note="robots.txt" if status == "blocked" else None)
            self.assertIn(availability_for("misc", record), AVAILABILITY_STATES)


class ContractEnvelopeShapeTests(unittest.TestCase):
    def _profile(self):
        return make_profile({
            "registry_live": evidence("registry_live", "available", "official_registry_live", "https://data.brreg.no/e", value={"name": "Example AS", "legal_form": "AS"}, content_sha256="a" * 64),
            "website": evidence("website", "blocked", "registry_linked_company_website", "https://example.no/", note="robots.txt disallows"),
        }, run_metrics={"requests": 3, "bytes": 500, "latencies_ms": [10, 20]})

    def test_required_top_level_fields_and_types(self):
        envelope = contract_envelope(self._profile(), run=RUN)
        for field in REQUIRED_TOP_LEVEL:
            self.assertIn(field, envelope)
        self.assertIsInstance(envelope["claims"], list)
        self.assertIsInstance(envelope["evidence"], list)
        self.assertIsInstance(envelope["changes"], list)
        self.assertIsInstance(envelope["errors"], list)
        self.assertIsInstance(envelope["run"], dict)
        self.assertIsInstance(envelope["operations"], dict)
        self.assertTrue(validate_contract_envelope(envelope)["passed"])

    def test_legal_identity_is_present(self):
        envelope = contract_envelope(self._profile(), run=RUN)
        self.assertEqual(envelope["company"]["organisation_number"], "923609016")
        self.assertEqual(envelope["company"]["name"], "Example AS")
        self.assertEqual(envelope["company"]["legal_form"], "AS")

    def test_evidence_references_and_source_snapshots(self):
        envelope = contract_envelope(self._profile(), run=RUN)
        ids = {entry["id"] for entry in envelope["evidence"]}
        for claim in envelope["claims"]:
            self.assertTrue(set(claim["evidence_ids"]).issubset(ids))
        registry = next(entry for entry in envelope["evidence"] if entry["id"] == "ev-registry_live")
        self.assertEqual(registry["source_url"], "https://data.brreg.no/e")
        self.assertEqual(registry["content_sha256"], "a" * 64)
        self.assertIsNotNone(registry["retrieved_at"])

    def test_blocked_source_is_reported_in_errors(self):
        envelope = contract_envelope(self._profile(), run=RUN)
        self.assertTrue(any(item["field"] == "website" and item["state"] == "blocked" for item in envelope["errors"]))

    def test_refresh_metadata_present(self):
        envelope = contract_envelope(self._profile(), run=RUN)
        self.assertIn("refresh", envelope)
        self.assertEqual(envelope["refresh"]["change_count"], 0)
        self.assertTrue(envelope["refresh"]["idempotent"])

    def test_operations_carry_cost_scope(self):
        envelope = contract_envelope(self._profile(), run=RUN)
        self.assertEqual(envelope["operations"]["requests"], 3)
        self.assertEqual(envelope["operations"]["third_party_cost_usd"], 0)
        self.assertEqual(envelope["operations"]["runtime_ms"], 30)

    def test_missing_and_absent_evidence_do_not_crash(self):
        envelope = contract_envelope(make_profile({}), run=RUN)
        self.assertEqual(envelope["claims"], [])
        self.assertEqual(envelope["evidence"], [])
        self.assertTrue(validate_contract_envelope(envelope)["passed"])

    def test_partial_failure_preserved(self):
        profile = make_profile({
            "registry_live": evidence("registry_live", "available", "official_registry_live", "https://data.brreg.no/e", value={"name": "Example AS"}),
            "roles": evidence("roles", "source_error", "official_roles", "https://data.brreg.no/r", note="HTTP 500"),
        })
        envelope = contract_envelope(profile, run=RUN)
        states = {claim["field"]: claim["availability"] for claim in envelope["claims"]}
        self.assertEqual(states["registry_live"], "available")
        self.assertEqual(states["roles"], "failed")

    def test_malformed_input_raises(self):
        with self.assertRaises(ValueError):
            contract_envelope(make_profile({}, org="123"), run=RUN)

    def test_serialization_round_trip(self):
        envelope = contract_envelope(self._profile(), run=RUN)
        text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(json.loads(text), envelope)

    def test_deterministic_output(self):
        first = json.dumps(contract_envelope(self._profile(), run=RUN), ensure_ascii=False, sort_keys=True)
        second = json.dumps(contract_envelope(self._profile(), run=RUN), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)


class BatchContractValidationTests(unittest.TestCase):
    def _envelope(self, org):
        return contract_envelope(make_profile({"registry_live": evidence("registry_live", "available", "official_registry_live", "https://data.brreg.no/e", value={"name": "X"})}, org=org), run=RUN)

    def test_exact_count_and_no_drops(self):
        envelopes = [self._envelope("923609016"), self._envelope("999999999")]
        self.assertTrue(validate_contract_envelopes(envelopes, 2)["passed"])

    def test_wrong_count_fails(self):
        envelopes = [self._envelope("923609016")]
        self.assertFalse(validate_contract_envelopes(envelopes, 2)["passed"])

    def test_duplicate_organisation_numbers_fail(self):
        envelopes = [self._envelope("923609016"), self._envelope("923609016")]
        result = validate_contract_envelopes(envelopes, 2)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["unique_organisation_numbers"])


class BatchRunnerContractTests(unittest.TestCase):
    ORG = "923609016"
    HEADER = [
        "organisasjonsnummer", "navn", "organisasjonsform.kode", "antallAnsatte", "konkurs",
        "underAvvikling", "forretningsadresse.kommune", "forretningsadresse.kommunenummer",
        "naeringskode1.kode", "naeringskode1.beskrivelse", "hjemmeside", "sisteInnsendteAarsregnskap",
    ]

    def _write_bulk(self, path: Path) -> None:
        with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(self.HEADER)
            for org, name in ((self.ORG, "Example AS"), ("999999999", "Other AS"), ("111111111", "Third AS")):
                writer.writerow([org, name, "AS", "10", "false", "false", "Oslo", "0301", "62.010", "IT", "https://example.no/", "2024"])

    def _fake_official(self, org, modules):
        catalog = {
            "registry_live": evidence("registry_live", "available", "official_registry_live", f"https://data.brreg.no/enheter/{org}", value={"name": "Example AS", "legal_form": "AS"}, content_sha256="a" * 64),
            "financials": evidence("financials", "not_found", "official_annual_accounts", f"https://data.brreg.no/regnskap/{org}", content_sha256="b" * 64),
            "roles": evidence("roles", "available", "official_roles", f"https://data.brreg.no/roller/{org}", value={"roles": []}, content_sha256="c" * 64),
            "group": evidence("group", "source_error", "official_group_structure", f"https://data.brreg.no/konsern/{org}", note="HTTP 500", content_sha256="d" * 64),
            "locations": evidence("locations", "not_applicable", "official_subunits", f"https://data.brreg.no/underenheter/{org}"),
        }
        records = {module: catalog[module] for module in modules if module in catalog}
        metrics = [FetchResult(url=record["source_url"], status=200, elapsed_ms=5, bytes_received=50, content_sha256=record["content_sha256"]) for record in records.values()]
        return records, metrics

    def _fake_website(self, url):
        value = {
            "requested_url": url,
            "final_url": url,
            "registered_domain": "example.no",
            "title": "Example AS",
            "main_text_excerpt": "Example AS, organisation number 923 609 016, is a company. " * 6,
            "social_links": [],
            "structured_organisations": [],
            "pages": [],
            "content_sha256": "e" * 64,
        }
        return evidence("website", "available", "registry_linked_company_website", url, value=value, content_sha256="e" * 64), {"requests": 1, "bytes": 100, "latencies_ms": [10]}

    def test_runner_emits_contract_envelopes_end_to_end(self):
        from scripts import run_competition_batch

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            bulk = work / "bulk.csv.gz"
            self._write_bulk(bulk)
            orgs = work / "orgs.jsonl"
            orgs.write_text(json.dumps({"organisation_number": self.ORG}) + "\n", encoding="utf-8")
            output = work / "envelopes.jsonl"
            profiles_output = work / "profiles.jsonl"
            report_path = work / "report.json"
            argv = [
                "run_competition_batch.py",
                "--organisations", str(orgs),
                "--bulk", str(bulk),
                "--output", str(output),
                "--profiles-output", str(profiles_output),
                "--report", str(report_path),
                "--run-id", "e2e-test",
                "--expected-count", "1",
                "--workers", "1",
            ]
            with patch.object(sys, "argv", argv), \
                    patch.object(run_competition_batch, "fetch_official_modules", self._fake_official), \
                    patch.object(run_competition_batch, "fetch_website", self._fake_website):
                with self.assertRaises(SystemExit) as context:
                    run_competition_batch.main()
            self.assertEqual(context.exception.code, 0)

            envelopes = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(envelopes), 1)
            self.assertEqual(envelopes[0]["organisation_number"], self.ORG)
            self.assertTrue(validate_contract_envelope(envelopes[0])["passed"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["validation"]["passed"])
            self.assertTrue(report["contract_validation"]["passed"])
            self.assertEqual(report["emitted_envelopes"], 1)
            states = {claim["field"]: claim["availability"] for claim in envelopes[0]["claims"]}
            self.assertEqual(states["registry_live"], "available")
            self.assertEqual(states["financials"], "not_available")
            self.assertEqual(states["group"], "failed")
            self.assertEqual(states["locations"], "not_applicable")
            self.assertEqual(states["website"], "available")


class ExistingFunctionalityTests(unittest.TestCase):
    def test_internal_envelope_api_still_valid(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {
                "registry": evidence("registry", "available", "official", "https://example.test", content_sha256="a" * 64),
                "website": evidence("website", "blocked", "company_site", "https://example.test", note="robots.txt denied"),
            },
        }
        envelope = terminal_envelope(profile, run_id="day-1", modules=["registry", "website"], started_at=RUN["started_at"], completed_at=RUN["completed_at"])
        self.assertTrue(validate_envelopes([envelope], 1)["passed"])
        contract = contract_envelope(profile, run=RUN, modules=["registry", "website"])
        self.assertTrue(validate_contract_envelope(contract)["passed"])


if __name__ == "__main__":
    unittest.main()
