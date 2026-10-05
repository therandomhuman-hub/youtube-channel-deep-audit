import subprocess, sys, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"runtime"/"request.py"

class RequestTests(unittest.TestCase):
    def execute(self, body):
        return subprocess.run([sys.executable,str(SCRIPT),"--issue-body",body],text=True,capture_output=True)
    def test_handle(self):
        r=self.execute("CHANNEL URL: https://www.youtube.com/@emmiescalm")
        self.assertEqual(r.returncode,0)
        self.assertIn("@emmiescalm",r.stdout)
    def test_video_rejected(self):
        r=self.execute("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertNotEqual(r.returncode,0)
    def test_two_channels_rejected(self):
        r=self.run("https://www.youtube.com/@a https://www.youtube.com/@b")
        self.assertNotEqual(r.returncode,0)
    def test_key_rejected(self):
        r=self.run("https://www.youtube.com/@a API_KEY=ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")
        self.assertNotEqual(r.returncode,0)
if __name__=="__main__":
    unittest.main()
