import json, tempfile, unittest, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runtime.validate import validate

class ValidatorTests(unittest.TestCase):
    def base_report(self):
        return {
            "metadata":{"methodology_version":"12.0-production","schema_version":"12.0.0","input_contract":{"api_key_exposed":False}},
            "channel":{},"coverage":{},"access_matrix":[],"snapshots":[],"videos":[{"id":"v1","transcript_audit":{"status":"NO_TRANSCRIPT_FOUND","text_retained":False},"video_analysis":{"original_evidence":{"video_id":"v1","url":"https://www.youtube.com/watch?v=v1","title":"Test","description":"","evidence_boundary":"test"},"audited_findings":{"summary":"No transcript available; audit bounded to public metadata."},"beginner_guidance":{"summary":"Use as a packaging example only; verify claims independently."},"decision":"RESEARCH MORE","confidence":"LOW","overall_beginner_rating":4.0}}],
            "posts":[],"comments_summary":[],"playlists":[],"channel_sections":[],"transcripts":[],"transcript_audit":{"target_video_count":1,"attempted_video_count":1,"full_transcript_count":0,"coverage_percent":100,"full_coverage_percent":0},"sources":[{"source_id":"s1","url":"https://example.com","role":"OFFICIAL_REFERENCE","captured_at":"2026-01-01T00:00:00Z"}],
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
            (root/"audit.html").write_text("""<!doctype html><html lang='en'><head></head><body><h1>x</h1>
<details class='video-card' data-video-id='v1'><summary>v1</summary>
<h3>Original public evidence</h3><p>Video ID: v1</p>
<h3>Audited findings</h3><p>Metadata-only bounded audit.</p><p>Transcript: NO_TRANSCRIPT_FOUND</p>
<h3>Beginner-friendly professional guidance</h3><p>RESEARCH MORE</p>
</details>
<script>localStorage.setItem('x','1');window.print();</script></body></html>""",encoding="utf-8")
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
