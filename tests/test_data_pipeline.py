"""Offline regressions: no financial model or live source is needed."""
from __future__ import annotations
import copy
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_data as build
import dtcc
import validate

# These are TEST FIXTURES for conversion mechanics, not certified production
# DTCC units. Production deliberately has no verified notation/economic rules.
TEST_RULES = {"decimal_fixture": {"multiplier_to_bp": 10000, "economic_meaning_verified": True},
              "bp_fixture": {"multiplier_to_bp": 1, "economic_meaning_verified": True}}
DAY = date(2026, 10, 16)


def row(rid="1", **changes):
    result = {
        "Dissemination Identifier": rid, "Original Dissemination Identifier": "",
        "Action type": "NEWT", "Event type": "TRAD", "Amendment indicator": "",
        "Execution Timestamp": "2026-10-16T15:21:52Z", "Event timestamp": "2026-10-16T15:22:00Z",
        "Expiration Date": "2031-12-20", "Underlying Asset Name": "NVIDIA Corp.",
        "UPI FISN": build.FISN, "Notional currency-Leg 1": "USD",
        "Spread-Leg 1": "0.0095", "Spread notation-Leg 1": "decimal_fixture",
        "Fixed rate-Leg 1": "0.01", "Other payment amount": "123456",
        "Other payment type": "UFRO", "Other payment currency": "USD",
    }
    result.update(changes)
    return result


def record(raw=None, day=DAY):
    return build.state_record(raw or row(), {"date": day.isoformat(), "url": dtcc._url(day)})


def payload(records, files=None, rules=TEST_RULES):
    return build.build_payload(records, files or [], start=DAY, end=DAY,
                               now="2026-10-19T00:00:00Z", rules=rules)


def fake_fetch(rows, status="downloaded", day=DAY, error=None):
    return dtcc.DayFetch(rows, {"date": day.isoformat(), "url": dtcc._url(day),
                               "status": status, "error": error, "fetched_at": "2026-10-19T00:00:00Z",
                               "checked_at": "2026-10-19T00:00:00Z"})


class SourceSpreadTests(unittest.TestCase):
    def inspect(self, raw, rules=TEST_RULES):
        return build.inspect_record(record(raw), rules=rules)

    def test_fixture_decimal_and_bp_units(self):
        self.assertEqual(self.inspect(row())["spread_bp"], 95)
        self.assertEqual(self.inspect(row(**{"Spread-Leg 1": "95", "Spread notation-Leg 1": "bp_fixture"}))["spread_bp"], 95)
        self.assertEqual(self.inspect(row(**{"Spread-Leg 1": "0"}))["spread_bp"], 0)

    def test_production_units_fail_closed_even_code_three(self):
        result = self.inspect(row(**{"Spread notation-Leg 1": "3"}), rules=build.NOTATION_RULES)
        self.assertIsNone(result["spread_bp"])
        self.assertIn("unverified_spread_notation", result["quality_reasons"])
        self.assertEqual(result["spread_legs"][0]["raw"], "0.0095")

    def test_unit_without_economic_meaning_is_not_enough(self):
        result = self.inspect(row(), {"decimal_fixture": {"multiplier_to_bp": 10000}})
        self.assertIsNone(result["spread_bp"])
        self.assertIn("unverified_spread_economic_meaning", result["quality_reasons"])

    def test_coupon_upfront_and_price_never_become_spread(self):
        result = self.inspect(row(**{"Spread-Leg 1": "", "Fixed rate-Leg 1": "0.05", "Price": "700"}))
        self.assertIsNone(result["spread_bp"])
        self.assertEqual(result["quality_reasons"], ["no_reported_spread"])
        self.assertEqual(result["fixed_rates"][0], "0.05")
        self.assertEqual(result["upfront"]["raw"], "123456")

    def test_no_arbitrary_outlier_filter(self):
        self.assertEqual(self.inspect(row(**{"Spread-Leg 1": "0.00001"}))["spread_bp"], 0.1)
        self.assertEqual(self.inspect(row(**{"Spread-Leg 1": "5"}))["spread_bp"], 50000)

    def test_invalid_nonfinite_missing_notation_and_conflicting_legs(self):
        for value in ("NaN", "Infinity", "1e999", "1,2", "500+", "garbage"):
            with self.subTest(value=value):
                self.assertIsNone(self.inspect(row(**{"Spread-Leg 1": value}))["spread_bp"])
        self.assertIsNone(self.inspect(row(**{"Spread notation-Leg 1": ""}))["spread_bp"])
        result = self.inspect(row(**{"Spread-Leg 2": "96", "Spread notation-Leg 2": "bp_fixture"}))
        self.assertIsNone(result["spread_bp"])
        self.assertIn("ambiguous_multi_leg_spread", result["quality_reasons"])

    def test_usd_and_spread_currency(self):
        for field in ("Notional currency-Leg 1", "Notional currency-Leg 2", "Spread currency-Leg 1"):
            with self.subTest(field=field):
                self.assertIsNone(self.inspect(row(**{field: "EUR"}))["spread_bp"])

    def test_exact_entity_and_seniority(self):
        for name in ("CoreWeave Financing-DDTL", "CoreWeave Inc2", "NVIDIA Financial Corp", "NVIDIA Corp Finance"):
            with self.subTest(name=name):
                result = self.inspect(row(**{"Underlying Asset Name": name}))
                self.assertIsNone(result["spread_bp"])
                self.assertIn("unmatched_exact_entity", result["quality_reasons"])
        self.assertEqual(self.inspect(row(**{"Underlying Asset Name": "CoreWeave Inc."}))["entity"], "COREWEAVE")
        self.assertIsNone(self.inspect(row(**{"UPI FISN": "NA/CDS Corp SN Sub"}))["spread_bp"])

    def test_actual_maturity_scope_and_timestamps_preserved(self):
        result = self.inspect(row())
        self.assertEqual(result["maturity_date"], "2031-12-20")
        self.assertGreater(result["remaining_years"], 5)
        self.assertEqual(result["source_event_timestamp"], "2026-10-16T15:22:00Z")
        self.assertEqual(result["source_event_timestamp_field"], "Event timestamp")
        self.assertIsNone(result["publication_delay_seconds"])
        self.assertIsNone(result["documentation"])
        for changes in ({"Expiration Date": "2038-12-20"}, {"Execution Timestamp": "2026-10-16T15:21:52"},
                        {"Event timestamp": "bad"}, {"Expiration Date": "bad"}):
            self.assertIsNone(self.inspect(row(**changes))["spread_bp"])

    def test_event_time_never_implies_dissemination_or_publication_delay(self):
        for event_time in ("2026-10-16T15:21:52Z", "2026-10-16T16:21:52Z"):
            with self.subTest(event_time=event_time):
                result = self.inspect(row(**{"Event timestamp": event_time}))
                self.assertEqual(result["source_event_timestamp"], event_time)
                self.assertIsNone(result["dissemination_timestamp"])
                self.assertIsNone(result["publication_delay_seconds"])
                self.assertEqual(result["publication_delay_status"], "unavailable_no_dissemination_timestamp")
                for old_field in ("report_timestamp", "report_timestamp_field", "report_delay_seconds"):
                    self.assertNotIn(old_field, result)

    def test_invalid_event_timestamp_has_event_specific_reason(self):
        result = self.inspect(row(**{"Event timestamp": "invalid"}))
        self.assertIn("invalid_source_event_timestamp", result["quality_reasons"])
        self.assertNotIn("invalid_report_timestamp", result["quality_reasons"])
        self.assertIsNone(result["source_event_timestamp"])
        self.assertIsNone(result["publication_delay_seconds"])


class LifecycleTests(unittest.TestCase):
    def test_identical_repeat_is_one_report_with_both_sources(self):
        result = payload([record(), record(day=date(2026, 10, 17))])
        self.assertEqual(result["entities"]["NVIDIA"]["trades"], 1)
        self.assertEqual(len(result["report_state"]), 1)
        self.assertEqual(len(result["report_state"][0]["source_files"]), 2)

    def test_conflicting_same_id_excluded(self):
        result = payload([record(), record(row(**{"Spread-Leg 1": "0.0100"}))])
        self.assertIsNone(result["entities"]["NVIDIA"]["last"])
        self.assertEqual(result["entities"]["NVIDIA"]["quality_counts"]["conflicting_duplicate_id"], 2)

    def test_correction_and_error_suppress_entire_chain(self):
        for action in ("CORR", "EROR", "TERM"):
            amended = row("2", **{"Original Dissemination Identifier": "1", "Action type": action})
            result = payload([record(), record(amended)])
            self.assertIsNone(result["entities"]["NVIDIA"]["last"])
            self.assertEqual(result["entities"]["NVIDIA"]["quality_counts"]["amended_lifecycle_unverified"], 2)

    def test_sparse_error_linked_across_days(self):
        sparse = record({"Dissemination Identifier": "2", "Original Dissemination Identifier": "1", "Action type": "EROR"})
        records = build.retain_relevant([record()], [sparse])
        self.assertEqual(len(records), 2)
        self.assertIsNone(payload(records)["entities"]["NVIDIA"]["last"])

    def test_error_missing_own_id_still_suppresses_original(self):
        sparse = record({"Dissemination Identifier": "", "Original Dissemination Identifier": "1", "Action type": "EROR"})
        result = payload([record(), sparse])
        self.assertIsNone(result["entities"]["NVIDIA"]["last"])
        self.assertIn("missing_report_id", result["entities"]["NVIDIA"]["quality_counts"])

    def test_daily_median_count_and_last_execution(self):
        second = row("2", **{"Spread-Leg 1": "0.0105", "Execution Timestamp": "2026-10-16T16:00:00Z",
                             "Event timestamp": "2026-10-16T16:01:00Z"})
        result = payload([record(), record(second)])
        nv = result["entities"]["NVIDIA"]
        self.assertEqual(nv["series"], [["2026-10-16", 100, 2]])
        self.assertEqual(nv["last_trade_timestamp"], "2026-10-16T16:00:00Z")
        self.assertEqual(nv["last_source_event_timestamp"], "2026-10-16T16:01:00Z")
        self.assertIsNone(nv["last_dissemination_timestamp"])
        self.assertIsNone(nv["last_publication_delay_seconds"])
        self.assertNotIn("last_report_timestamp", nv)
        self.assertNotIn("last_report_delay_seconds", nv)
        self.assertEqual(len(result["entities"]), 12)
        self.assertIn("COREWEAVE", result["missing_entities"])


class IncrementalTests(unittest.TestCase):
    def test_late_report_augments_full_older_execution_day(self):
        old = payload([record()])
        late_day = date(2026, 10, 18)
        late = row("2", **{"Spread-Leg 1": "0.0105", "Event timestamp": "2026-10-18T16:00:00Z"})
        records, files = build.collect(late_day, late_day, old, fetcher=lambda d, **kw: fake_fetch([late], day=d))
        result = payload(records, files)
        self.assertEqual(result["entities"]["NVIDIA"]["series"], [["2026-10-16", 100, 2]])
        self.assertEqual(files[0]["date"], "2026-10-18")  # Sunday is scanned.

    def test_late_sparse_error_removes_old_day(self):
        old = payload([record()])
        late_day = date(2026, 10, 18)
        sparse = {"Dissemination Identifier": "2", "Original Dissemination Identifier": "1", "Action type": "EROR"}
        records, files = build.collect(late_day, late_day, old, fetcher=lambda d, **kw: fake_fetch([sparse], day=d))
        self.assertIsNone(payload(records, files)["entities"]["NVIDIA"]["last"])

    def test_legacy_series_never_survives_migration(self):
        old = {"entities": {"NVIDIA": {"series": [["2025-01-01", 987, 1]]}}}
        self.assertFalse(build.compatible(old))
        records, files = build.collect(DAY, DAY, old, fetcher=lambda d, **kw: fake_fetch([], day=d))
        self.assertEqual(payload(records, files)["entities"]["NVIDIA"]["series"], [])

    def test_refresh_replaces_whole_file_and_failure_preserves_history(self):
        old = payload([record()])
        records, _ = build.collect(DAY, DAY, old, fetcher=lambda d, **kw: fake_fetch([], day=d))
        self.assertEqual(records, [])
        records, files = build.collect(DAY, DAY, old, fetcher=lambda d, **kw: fake_fetch(None, "error", d, "timeout"))
        self.assertEqual(len(records), 1)
        result = payload(records, files)
        self.assertEqual(result["coverage"]["failed_files"][0]["error"], "timeout")

    def test_validator_rejects_legacy_and_injected_model_values(self):
        self.assertTrue(validate.validate({"entities": {}}))
        actual = payload([record()], rules={})
        self.assertFalse(validate.validate(actual))
        actual["entities"]["NVIDIA"]["series"] = [["2026-10-16", 100, 1]]
        self.assertTrue(validate.validate(actual))

    def test_existing_v2_raw_state_rebuild_removes_old_timestamp_semantics(self):
        old = payload([record()], rules={})
        del old["timestamp_policy"]
        nv = old["entities"]["NVIDIA"]
        nv["last_report_timestamp"] = "2026-10-16T15:22:00Z"
        nv["last_report_delay_seconds"] = 8
        nv["latest_report"].update(report_timestamp="2026-10-16T15:22:00Z",
                                   report_timestamp_field="Event timestamp", report_delay_seconds=8)
        self.assertTrue(build.compatible(old))  # Source state remains reusable.
        self.assertTrue(validate.validate(old))  # Publication contract is obsolete.
        late_day = date(2026, 10, 18)
        records, files = build.collect(late_day, late_day, old,
                                       fetcher=lambda d, **kw: fake_fetch([], day=d))
        self.assertEqual(records, old["report_state"])
        rebuilt = payload(records, files, rules={})
        self.assertEqual(rebuilt["schema_version"], 2)
        self.assertEqual(rebuilt["timestamp_policy"], "event-time-is-not-publication-time")
        self.assertFalse(validate.validate(rebuilt))
        forbidden = {"report_timestamp", "report_timestamp_field", "report_delay_seconds",
                     "last_report_timestamp", "last_report_delay_seconds"}
        def check_tree(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value))
                for child in value.values():
                    check_tree(child)
            elif isinstance(value, list):
                for child in value:
                    check_tree(child)
        check_tree(rebuilt)

    def test_validator_requires_timestamp_policy_even_for_all_null_data(self):
        actual = payload([record()], rules={})
        self.assertFalse(validate.validate(actual))
        del actual["timestamp_policy"]
        self.assertTrue(validate.validate(actual))

    def test_latest_raw_spread_is_separate_from_coupon_only_latest_report(self):
        latest = row("2", **{"Spread-Leg 1": "", "Event timestamp": "2026-10-18T18:00:00Z",
                             "Execution Timestamp": "2026-10-18T17:59:00Z"})
        result = payload([record(), record(latest)], rules={})
        nv = result["entities"]["NVIDIA"]
        self.assertEqual(nv["latest_report"]["report_id"], "2")
        self.assertEqual(nv["latest_spread_report"]["report_id"], "1")
        self.assertIsNone(nv["latest_spread_report"]["spread_bp"])

    def test_split_state_roundtrip_and_tampering_rejection(self):
        cache = Path(__file__).resolve().parents[1] / "cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as temp:
            self.assertTrue(Path(temp).resolve().is_relative_to(cache.resolve()))
            out = Path(temp) / "cds.json"
            state = Path(temp) / "report-state.json"
            original = payload([record()], rules={})
            build.write_artifacts(original, str(out), str(state))
            self.assertNotIn("report_state", json.loads(out.read_text(encoding="utf-8")))
            loaded = build.load_artifacts(str(out))
            self.assertEqual(loaded["report_state"], original["report_state"])
            self.assertFalse(validate.validate(loaded))
            state.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash"):
                build.load_artifacts(str(out))


class FetchTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1] / "cache"
        cache.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(cache.resolve()))
        self.cache_patch = patch.object(dtcc, "CACHE", self.temp.name)
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.temp.cleanup()

    def archive(self, text="Dissemination Identifier,Action type\n1,NEWT\n"):
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, "w") as zf:
            zf.writestr("source.csv", text)
        return blob.getvalue()

    def cached(self):
        Path(self.temp.name, f"{DAY:%Y_%m_%d}.csv").write_text("Dissemination Identifier,Action type\n1,NEWT\n")

    def response(self, blob):
        return io.BytesIO(blob)

    def test_historical_cache_frozen_current_day_refreshes(self):
        self.cached()
        with patch.object(dtcc.urllib.request, "urlopen", return_value=self.response(self.archive())) as opened:
            self.assertEqual(dtcc.fetch_day_result(DAY, today=date(2026, 10, 19)).metadata["status"], "cached")
            opened.assert_not_called()
            self.assertEqual(dtcc.fetch_day_result(DAY, today=DAY).metadata["status"], "downloaded")
            opened.assert_called_once()

    def test_prior_two_dates_refreshed_and_cache_time_not_fetch_time(self):
        self.cached()
        with patch.object(dtcc.urllib.request, "urlopen", side_effect=lambda *a, **kw: self.response(self.archive())) as opened:
            for today in (date(2026, 10, 17), date(2026, 10, 18)):
                self.assertEqual(dtcc.fetch_day_result(DAY, today=today).metadata["status"], "downloaded")
            self.assertEqual(opened.call_count, 2)
            old = dtcc.fetch_day_result(DAY, today=date(2026, 10, 19))
            self.assertEqual(old.metadata["status"], "cached")
            self.assertIsNone(old.metadata["fetched_at"])
            self.assertIsNotNone(old.metadata["cache_modified_at"])
            self.assertEqual(len(old.metadata["csv_sha256"]), 64)

    def test_transient_failure_retries_then_succeeds(self):
        with patch.object(dtcc.urllib.request, "urlopen", side_effect=[urllib.error.URLError("temporary"), self.response(self.archive())]) as opened, patch.object(dtcc.time, "sleep"):
            result = dtcc.fetch_day_result(DAY, today=DAY)
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(result.metadata["status"], "downloaded")

    def test_failed_current_refresh_is_explicit_stale_cache(self):
        self.cached()
        with patch.object(dtcc.urllib.request, "urlopen", side_effect=urllib.error.URLError("temporary")), patch.object(dtcc.time, "sleep"):
            result = dtcc.fetch_day_result(DAY, today=DAY)
        self.assertEqual(result.metadata["status"], "stale_cache")
        self.assertIn("temporary", result.metadata["error"])
        self.assertEqual(len(result.rows), 1)

    def test_missing_file_differs_from_network_failure(self):
        error = urllib.error.HTTPError("https://example.invalid", 404, "missing", {}, None)
        with patch.object(dtcc.urllib.request, "urlopen", side_effect=error):
            result = dtcc.fetch_day_result(DAY, today=DAY)
        self.assertEqual(result.metadata["status"], "not_found")
        self.assertIsNone(result.rows)

    def test_header_only_weekend_file_valid_zero_byte_invalid(self):
        with patch.object(dtcc.urllib.request, "urlopen", return_value=self.response(self.archive("Dissemination Identifier,Action type\n"))):
            result = dtcc.fetch_day_result(DAY, today=DAY)
        self.assertEqual(result.rows, [])
        self.assertEqual(result.metadata["status"], "downloaded")
        with patch.object(dtcc.urllib.request, "urlopen", return_value=self.response(self.archive(""))):
            result = dtcc.fetch_day_result(DAY, today=DAY, use_cache=False, attempts=1)
        self.assertEqual(result.metadata["status"], "error")


if __name__ == "__main__":
    unittest.main()
