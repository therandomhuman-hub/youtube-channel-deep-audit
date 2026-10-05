import json, os, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runtime.youtube_audit import METHOD_COSTS, SEARCH_CALL_BUDGET, Quota, Checkpoint, extract_channel_ref, plausible_key
from runtime.transcript_audit import make_transcript_record, audit_learning, build_claim_evidence_matrix, _apply_transcript_sanity

class RuntimeTests(unittest.TestCase):
    def test_quota_costs(self):
        self.assertEqual(METHOD_COSTS["channels.list"],1)
        self.assertEqual(METHOD_COSTS["playlistItems.list"],1)
        self.assertEqual(METHOD_COSTS["videos.list"],1)
        self.assertEqual(METHOD_COSTS["commentThreads.list"],1)
        self.assertEqual(METHOD_COSTS["comments.list"],1)
        self.assertEqual(METHOD_COSTS["search.list"],1)
        self.assertEqual(SEARCH_CALL_BUDGET,100)

    def test_identity_inputs(self):
        self.assertEqual(extract_channel_ref("https://www.youtube.com/@emmiescalm"),("handle","@emmiescalm"))
        self.assertEqual(extract_channel_ref("https://www.youtube.com/channel/UC1234567890123456789012"),("id","UC1234567890123456789012"))
        self.assertEqual(extract_channel_ref("@abc"),("handle","@abc"))

    def test_key_is_plausible(self):
        self.assertTrue(plausible_key("A"*30))
        self.assertFalse(plausible_key("short"))
        self.assertFalse(plausible_key("A"*30+" "))


    def test_transcript_record_is_bounded(self):
        rec = make_transcript_record(
            "abc123456",
            [{"text":"I made $10,000 in 30 days. First, create the channel, then publish original videos.", "start":0, "duration":4}],
            source="TEST",
            language="English",
            language_code="en",
            is_generated=False,
        )
        self.assertEqual(rec["text_retained"], False)
        self.assertTrue(rec["transcript_sha256"])
        self.assertGreaterEqual(rec["claim_count"], 1)
        self.assertLessEqual(len(rec["bounded_excerpt"].split()), 20)

    def test_transcript_score_requires_full_transcript_for_high_confidence(self):
        partial = audit_learning(
            title="How I made $10K",
            description="",
            transcript={"status":"NO_TRANSCRIPT_FOUND"},
        )
        self.assertEqual(partial["confidence"], "LOW")
        self.assertNotEqual(partial["decision"], "KEEP")

    def test_full_transcript_learning_audit_has_individual_scores(self):
        rec = make_transcript_record(
            "abc123456",
            [{"text":"First set up the channel. Then test three original videos and measure analytics. I show the dashboard and the date range.", "start":0, "duration":6}],
            source="TEST",
            language="English",
            language_code="en",
            is_generated=True,
        )
        audit = audit_learning(title="Full system", description="", transcript=rec)
        for key in [
            "overall_beginner_rating","practical_usefulness","evidence_discipline",
            "beginner_accessibility","repeatability","originality_safety","policy_safety",
            "policy_risk","confidence","decision","learning_mode","claim_evidence_gap"
        ]:
            self.assertIn(key, audit)
        self.assertEqual(audit["confidence"], "HIGH")

    def test_quota_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            cp=Checkpoint(Path(d)/"checkpoint.json")
            q=Quota(cp,9000)
            self.assertTrue(q.charge("channels.list",1))
            q2=Quota(cp,9000)
            self.assertEqual(q2.used,1)

    def test_claim_evidence_matrix_never_treats_transcript_as_truth(self):
        rec = make_transcript_record(
            "abc123456",
            [{"text":"I made $75,000 in 90 days and got monetized in 7 days. First, create original videos.", "start":0, "duration":6}],
            source="TEST",
            language="English",
            language_code="en",
            is_generated=False,
        )
        matrix = build_claim_evidence_matrix(rec["claims"], rec["proof_signal_count"], rec["status"])
        self.assertTrue(matrix)
        self.assertTrue(all(x["independent_verification"] == "REQUIRED" for x in matrix))
        self.assertTrue(any(x["high_impact"] for x in matrix))
        self.assertTrue(all(x["verification_state"] == "NOT_INDEPENDENTLY_VERIFIED" for x in matrix))

    def test_embed_fallback_is_part_of_production_runtime(self):
        from runtime.youtube_audit import _embed_transcript_fallback
        self.assertTrue(callable(_embed_transcript_fallback))

    def test_transcript_sanity_downgrades_implausible_density(self):
        rec = make_transcript_record('abc123456', [{'text':'word '*1000, 'start':0, 'duration':0}], source='TEST')
        checked = _apply_transcript_sanity(rec, 120)
        self.assertEqual(checked['quality_grade'], 'QUESTIONABLE')
        self.assertFalse(checked['transcript_sanity']['plausible'])

if __name__=="__main__":
    unittest.main()
