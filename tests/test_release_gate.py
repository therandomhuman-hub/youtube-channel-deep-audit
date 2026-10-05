import unittest
from runtime.build_release import release_allowed

class ReleaseGateTests(unittest.TestCase):
    def test_only_clean_pass_is_releaseable(self):
        self.assertTrue(release_allowed({"status": "PASS"}))
        self.assertFalse(release_allowed({"status": "WARN"}))
        self.assertFalse(release_allowed({"status": "FAIL"}))
        self.assertFalse(release_allowed({}))

if __name__ == "__main__":
    unittest.main()
