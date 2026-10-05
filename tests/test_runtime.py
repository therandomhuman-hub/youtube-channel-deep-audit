import json, os, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runtime.youtube_audit import METHOD_COSTS, SEARCH_CALL_BUDGET, Quota, Checkpoint, extract_channel_ref, plausible_key

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

    def test_quota_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            cp=Checkpoint(Path(d)/"checkpoint.json")
            q=Quota(cp,9000)
            self.assertTrue(q.charge("channels.list",1))
            q2=Quota(cp,9000)
            self.assertEqual(q2.used,1)

if __name__=="__main__":
    unittest.main()
