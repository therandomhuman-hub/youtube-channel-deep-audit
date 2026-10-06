import hashlib, json, tempfile, unittest
from pathlib import Path
from runtime.validate import validate
from tests.test_validator import ValidatorTests

class HtmlCardReconciliationTests(unittest.TestCase):
    def _package(self, root: Path, html: str):
        report = ValidatorTests().base_report()
        (root / "audit.json").write_text(json.dumps(report), encoding="utf-8")
        (root / "schema.json").write_text(json.dumps({"type":"object","required":[],"properties":{}}), encoding="utf-8")
        (root / "audit.html").write_text(html, encoding="utf-8")
        (root / "video_inventory.csv").write_text("video_id\nv1\n", encoding="utf-8")
        (root / "comments.csv").write_text("video_id,kind,comment_id,text,published_at\n", encoding="utf-8")
        (root / "comments_coverage.csv").write_text("video_id,top_level_threads,replies_collected,complete,reply_pages,coverage\nv1,0,0,true,0,100\n", encoding="utf-8")
        (root / "sources.csv").write_text("source_id,url,role,captured_at\ns1,https://example.com,OFFICIAL_REFERENCE,2026-01-01\n", encoding="utf-8")
        (root / "config.yaml").write_text("x: 1\n", encoding="utf-8")
        hashes = {}
        for p in root.iterdir():
            if p.name != "release_manifest.json":
                hashes[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        (root / "release_manifest.json").write_text(json.dumps({"files": hashes}), encoding="utf-8")

    def test_missing_video_card_is_release_issue(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._package(root, "<!doctype html><html lang='en'><body><h1>x</h1></body></html>")
            issues, _ = validate(root)
            self.assertTrue(any("video card count" in x["message"] for x in issues))

    def test_card_missing_required_layer_is_issue(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            html = """<!doctype html><html lang='en'><body><h1>x</h1>
<details class='video-card'><summary>v1</summary>
<h3>1. Original public evidence</h3>
<h3>2. Audited findings</h3>
<a href='https://www.youtube.com/watch?v=v1'>Open original YouTube video</a>
</details></body></html>"""
            self._package(root, html)
            issues, _ = validate(root)
            self.assertTrue(any("BEGINNER-FRIENDLY PROFESSIONAL GUIDANCE" in x["message"] for x in issues))


    def test_nested_details_are_parsed_as_one_complete_card(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            html = """<!doctype html><html lang='en'><body><h1>x</h1>
<details class='video-card'><summary>v1</summary>
<h3>Original public evidence</h3><details><summary>nested</summary><p>inner</p></details>
<h3>Audited findings</h3><p>audit</p>
<h3>Beginner-friendly professional guidance</h3><p>guide</p>
<p>Transcript: NO_TRANSCRIPT_FOUND</p>
<a href='https://www.youtube.com/watch?v=abcdefghijk'>Open original YouTube video</a>
</details></body></html>"""
            self._package(root, html)
            issues, _ = validate(root)
            self.assertFalse(any("video card count" in x["message"] for x in issues))
            self.assertFalse(any("mandatory visible layer missing" in x["message"] for x in issues))
            self.assertFalse(any("video ID mismatch" in x["message"] for x in issues))

if __name__ == "__main__":
    unittest.main()
