import unittest
from runtime.release_contract import (
    CANONICAL_METHODOLOGY_VERSION,
    CANONICAL_SCHEMA_ID,
    CANONICAL_SCHEMA_TITLE,
    errors,
    is_valid,
)

class CanonicalReleaseContractTests(unittest.TestCase):
    def test_canonical_identity_passes(self):
        report={"metadata":{"methodology_version":"12.1","schema_version":"12.1"}}
        schema={"$id":CANONICAL_SCHEMA_ID,"title":CANONICAL_SCHEMA_TITLE}
        self.assertEqual(errors(report,schema),[])
        self.assertTrue(is_valid(report,schema))

    def test_stale_methodology_is_blocked(self):
        report={"metadata":{"methodology_version":"12.0-production","schema_version":"12.1"}}
        schema={"$id":CANONICAL_SCHEMA_ID,"title":CANONICAL_SCHEMA_TITLE}
        self.assertFalse(is_valid(report,schema))
        self.assertIn("methodology_version", " ".join(errors(report,schema)))

    def test_stale_schema_identity_is_blocked(self):
        report={"metadata":{"methodology_version":"12.1","schema_version":"12.1"}}
        schema={"$id":"youtube-channel-deep-audit-v12.0.schema.json","title":"YouTube Channel Deep Audit v11.5 Production Report"}
        self.assertFalse(is_valid(report,schema))
        self.assertEqual(len(errors(report,schema)),2)

if __name__=="__main__":
    unittest.main()
