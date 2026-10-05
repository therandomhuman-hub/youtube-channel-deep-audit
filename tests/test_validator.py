import json, tempfile, unittest, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runtime.validate import validate

class ValidatorTests(unittest.TestCase):
    def base_report(self):
        return {
            "metadata":{"methodology_version":"11.3-production","schema_version":"11.3.0","input_contract":{"api_key_exposed":False}},
            "channel":{},"coverage":{},"access_matrix":[],"snapshots":[],"videos":[{"id":"v1"}],
            "posts":[],"comments_summary":[],"playlists":[],"channel_sections":[],"transcripts":[],"transcript_audit":{"target_video_count":1,"attempted_video_count":0,"full_transcript_count":0,"coverage_percent":0,"full_coverage_percent":0},"sources":[{"source_id":"s1","url":"https://example.com","role":"OFFICIAL_REFERENCE","captured_at":"2026-01-01T00:00:00Z"}],
            "claims":[],"metrics":[],"calculations":[],"risks":[],"hypotheses":[],"experiments":[],"recommendations":[],"benchmarks":[],"knowledge_gaps":[],"deltas":[],"policy_checks":[],
            "decision_queue":{},"validation":{"status":"PARTIAL"},"executive_summary":{},"beginner_plan":{},"analysis":{},"reproducibility":{"credential_exposed":False},"self_audit":{"api_key_exposed":False}
        }
    def test_missing_artifact_is_issue(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            issues,warnings=validate(root)
            self.assertTrue(issues)
    def test_secret_in_package_is_issue(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            report=self.base_report()
            report["videos"][0]["transcript_audit"]={"status":"NO_TRANSCRIPT_FOUND","text_retained":False}
            for name,obj in [("audit.json",report),("schema.json",{"type":"object","required":[],"properties":{}})]:
                (root/name).write_text(json.dumps(obj),encoding="utf-8")
            (root/"audit.html").write_text("<!doctype html><html lang='en'><head></head><body><h1>x</h1><script>const a='AIzaAAAAAAAAAAAAAAAAAAAAAAA';</script></body></html>",encoding="utf-8")
            (root/"video_inventory.csv").write_text("video_id\nv1\n",encoding="utf-8")
            (root/"comments.csv").write_text("video_id,kind,comment_id,text,published_at\n",encoding="utf-8")
            (root/"comments_coverage.csv").write_text("video_id,top_level_threads,replies_collected,complete,reply_pages,coverage\n",encoding="utf-8")
            (root/"sources.csv").write_text("source_id,url,role,captured_at\ns1,https://example.com,OFFICIAL_REFERENCE,2026-01-01\n",encoding="utf-8")
            (root/"config.yaml").write_text("x: 1\n",encoding="utf-8")
            (root/"release_manifest.json").write_text(json.dumps({"files":{}}),encoding="utf-8")
            issues,_=validate(root)
            self.assertTrue(any("secret-like" in x["message"] for x in issues))
    def test_valid_minimal_package(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); report=self.base_report()
            report["videos"][0]["transcript_audit"]={"status":"NO_TRANSCRIPT_FOUND","text_retained":False}
            (root/"audit.json").write_text(json.dumps(report),encoding="utf-8")
            (root/"schema.json").write_text(json.dumps({"type":"object","required":[],"properties":{}}),encoding="utf-8")
            (root/"audit.html").write_text("<!doctype html><html lang='en'><head></head><body><h1>x</h1><script>localStorage.setItem('x','1');window.print();</script></body></html>",encoding="utf-8")
            (root/"video_inventory.csv").write_text("video_id\nv1\n",encoding="utf-8")
            (root/"comments.csv").write_text("video_id,kind,comment_id,text,published_at\n",encoding="utf-8")
            (root/"comments_coverage.csv").write_text("video_id,top_level_threads,replies_collected,complete,reply_pages,coverage\n",encoding="utf-8")
            (root/"sources.csv").write_text("source_id,url,role,captured_at\ns1,https://example.com,OFFICIAL_REFERENCE,2026-01-01\n",encoding="utf-8")
            (root/"config.yaml").write_text("x: 1\n",encoding="utf-8")
            hashes={}
            import hashlib
            for p in root.iterdir():
                if p.name=="release_manifest.json": continue
                h=hashlib.sha256(p.read_bytes()).hexdigest(); hashes[p.name]=h
            (root/"release_manifest.json").write_text(json.dumps({"files":hashes}),encoding="utf-8")
            issues,_=validate(root)
            self.assertEqual(issues,[])
if __name__=="__main__":
    unittest.main()
