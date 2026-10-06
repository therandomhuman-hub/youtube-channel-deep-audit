import unittest
from runtime.validate import CANONICAL_VERSION, CANONICAL_SCHEMA_ID, CANONICAL_SCHEMA_TITLE
class CanonicalContractTests(unittest.TestCase):
    def test_canonical_contract(self):
        self.assertEqual(CANONICAL_VERSION, '12.1')
        self.assertEqual(CANONICAL_SCHEMA_ID, 'youtube-channel-deep-audit-v12.1.schema.json')
        self.assertEqual(CANONICAL_SCHEMA_TITLE, 'YouTube Channel Deep Audit v12.1 Production Report')
if __name__ == '__main__': unittest.main()
