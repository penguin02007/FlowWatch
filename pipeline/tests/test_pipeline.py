"""Run with: python -m unittest discover -s tests (from the pipeline directory)."""
import unittest
from datetime import datetime, timedelta, timezone

from flowwatch.codec import Decoder, FlowRecord, encode_ipfix, encode_v9
from flowwatch.topology import EXPORTERS_BY_NAME, classify
from flowwatch.traffic import anchor_for, history_scenarios, samples_at
from flowwatch.tsds import Rollup

RECORDS = [
    FlowRecord("10.10.1.21", "142.250.72.14", 51000, 443, 6, 1_500_000, 1200, 3, 1, 1_790_000_000_000, 1_790_000_005_000, 0x18),
    FlowRecord("52.112.4.5", "10.10.1.50", 3478, 60000, 17, 900_000, 800, 1, 3, 1_790_000_001_000, 1_790_000_005_000),
]


class CodecTest(unittest.TestCase):
    def test_v9_roundtrip_needs_template(self):
        dec = Decoder()
        unix_secs = 1_790_000_005
        kwargs = dict(source_id=101, sequence=0, unix_secs=unix_secs, sys_uptime_ms=86_400_000)
        self.assertEqual(dec.decode(encode_v9(RECORDS, with_template=False, **kwargs), "1.1.1.1")[2], [])
        proto, domain, out = dec.decode(encode_v9(RECORDS, with_template=True, **kwargs), "1.1.1.1")
        self.assertEqual((proto, domain), ("netflow_v9", 101))
        self.assertEqual(out, RECORDS)
        # Subsequent packets without a template decode from the cache.
        self.assertEqual(dec.decode(encode_v9(RECORDS, with_template=False, **kwargs), "1.1.1.1")[2], RECORDS)

    def test_ipfix_roundtrip(self):
        dec = Decoder()
        pkt = encode_ipfix(RECORDS, domain_id=201, sequence=5, export_secs=1_790_000_005, with_template=True)
        proto, domain, out = dec.decode(pkt, "2.2.2.2")
        self.assertEqual((proto, domain), ("ipfix", 201))
        self.assertEqual(out, RECORDS)


class ModelTest(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(classify("tcp", 443, "10.10.1.30", "52.96.12.10"), "microsoft-365")
        self.assertEqual(classify("tcp", 443, "10.10.1.21", "142.250.72.14"), "web-https")
        self.assertEqual(classify("tcp", 873, "10.30.5.20", "10.40.5.20"), "backup-rsync")

    def test_links_never_exceed_capacity(self):
        now = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
        samples = samples_at(now, anchor_for(now), [("smb-bulk-copy", 1.0), ("backup-overrun", 1.0)])
        mpls_out = sum(s.bps for s in samples if s.exporter == "edge-rtr-01" and s.out_if == 2)
        cap = EXPORTERS_BY_NAME["edge-rtr-01"].interface(2).capacity_bps
        self.assertLessEqual(mpls_out, cap)
        self.assertGreater(mpls_out, 0.9 * cap)

    def test_history_incidents(self):
        setup_at = datetime(2026, 9, 29, 6, 7, tzinfo=timezone.utc)
        anchor = anchor_for(setup_at)
        # The earlier-today incident ends at least 30 minutes before setup, on a quarter hour:
        # 05:37 rounds down to 05:30, so it runs 05:00-05:30.
        incident_start = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)
        for t, active in ((incident_start - timedelta(minutes=1), False),
                          (incident_start, True),
                          (incident_start + timedelta(minutes=29), True),
                          (incident_start + timedelta(minutes=30), False)):
            self.assertEqual(("smb-bulk-copy", 1.0) in history_scenarios(t, anchor, setup_at), active, t)
        # Fixed-time incidents still follow the anchor day.
        t = anchor - timedelta(days=5) + timedelta(hours=16, minutes=30)
        self.assertIn(("update-storm", 1.0), history_scenarios(t, anchor, setup_at))

    def test_rollup_merges_identical_dimensions(self):
        r = Rollup()
        for _ in range(3):
            r.add(0, "edge-rtr-01", 3, 1, "10.10.1.21", "142.250.72.14", 51000, 443, 6, 100, 1)
        docs = r.pop_documents(0)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["network"]["bytes"], 300)
        self.assertEqual(docs[0]["interface"]["out"]["name"], "Gi0/0/0")
        self.assertEqual(docs[0]["service"]["port"], 443)


if __name__ == "__main__":
    unittest.main()
