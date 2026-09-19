import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gold_system.core import D
from gold_system.evidence import EvidenceArchive, QuantitativeEvidence
from gold_system.store import Store


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"evidence.db")
        self.archive = EvidenceArchive(self.store)
        self.now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        self.evidence = QuantitativeEvidence("release1", "SYNTHETIC", "first", "https://example.com/release",
            self.now-timedelta(days=1), self.now, self.now+timedelta(seconds=2), D("2.1"), "percent", "a"*64)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def select(self, cutoff):
        return self.archive.as_of("SYNTHETIC", cutoff, timedelta(days=30))

    def test_release_is_unavailable_until_received(self):
        self.archive.append(self.evidence)
        self.assertIsNone(self.select(self.now-timedelta(seconds=1)))
        self.assertIsNone(self.select(self.now+timedelta(seconds=1)))
        self.assertEqual(self.select(self.now+timedelta(seconds=2)), self.evidence)

    def test_revision_does_not_leak_backwards(self):
        self.archive.append(self.evidence)
        revision = replace(self.evidence, evidence_id="release2", vintage="revised", actual=D("2.5"),
                           released_at=self.now+timedelta(days=2), received_at=self.now+timedelta(days=2, seconds=1))
        self.archive.append(revision)
        self.assertEqual(self.select(self.now+timedelta(days=1)).actual, D("2.1"))
        self.assertEqual(self.select(self.now+timedelta(days=3)).actual, D("2.5"))

    def test_archive_cannot_overwrite_identity(self):
        self.assertTrue(self.archive.append(self.evidence))
        self.assertFalse(self.archive.append(self.evidence))
        with self.assertRaisesRegex(ValueError, "content conflict"):
            self.archive.append(replace(self.evidence, actual=D(7)))
        self.assertEqual(self.select(self.now+timedelta(days=1)).actual, D("2.1"))

    def test_same_release_conflicting_values_are_not_arbitrarily_selected(self):
        self.archive.append(self.evidence)
        self.archive.append(replace(self.evidence, evidence_id="conflict", actual=D(3)))
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.select(self.now+timedelta(days=1))

    def test_timestamp_age_actual_and_source_validation(self):
        for changes in ({"released_at": self.now.replace(tzinfo=None)}, {"received_at": self.now-timedelta(seconds=1)},
                        {"actual": D("NaN")}, {"unit": ""}, {"source": "https://user:password@example.com"},
                        {"raw_sha256": ""}, {"observed_at": self.now+timedelta(days=1)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.archive.append(replace(self.evidence, **changes))
        self.archive.append(self.evidence)
        self.assertIsNone(self.select(self.now+timedelta(days=31)))
        with self.assertRaises(ValueError):
            self.archive.as_of("SYNTHETIC", self.now, timedelta(0))

    def test_disk_payload_corruption_is_not_silently_used(self):
        self.archive.append(self.evidence)
        self.store.db.execute("UPDATE quantitative_evidence SET payload='{}'")
        self.store.db.commit()
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.select(self.now+timedelta(days=1))

    def test_restart_retains_original_received_time(self):
        self.archive.append(self.evidence)
        self.store.close()
        self.store = Store(Path(self.tmp.name)/"evidence.db")
        self.archive = EvidenceArchive(self.store)
        self.assertIsNone(self.select(self.now))
        self.assertEqual(self.select(self.now+timedelta(days=1)).received_at, self.evidence.received_at)

    def test_completeness_counts_only_point_in_time_eligible_records(self):
        self.archive.append(self.evidence)
        weights = {"optional_a": D(3), "optional_b": D(2)}
        ages = {name: timedelta(days=30) for name in ("SYNTHETIC", *weights)}
        self.archive.append(replace(self.evidence, evidence_id="opt_a", series="optional_a"))
        cutoff = self.now+timedelta(days=1)
        result = self.archive.context(["SYNTHETIC"], weights, ages, cutoff, "test-v1")
        self.assertEqual(result["weighted_missingness"], D("0.4"))
        self.assertFalse(result["evidence_eligible"])
        self.archive.append(replace(self.evidence, evidence_id="opt_b", series="optional_b",
                                   received_at=cutoff+timedelta(seconds=1)))
        self.assertFalse(self.archive.context(["SYNTHETIC"], weights, ages, cutoff, "test-v1")["evidence_eligible"])
        result = self.archive.context(["SYNTHETIC"], weights, ages, cutoff+timedelta(seconds=1), "test-v1")
        self.assertTrue(result["evidence_eligible"])
        self.assertFalse(result["entries_enabled"])
        result = self.archive.context(["SYNTHETIC"], weights, ages, self.now, "test-v1")
        self.assertEqual(result["missing_required"], ["SYNTHETIC"])
        self.assertFalse(result["evidence_eligible"])
