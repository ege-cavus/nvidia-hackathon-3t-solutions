"""Check the published risk matrix without claiming a station risk mapping."""
import unittest
from response_benchmarks import fire_response_reference


class FireResponseReferenceTests(unittest.TestCase):
    def test_all_nine_historical_targets(self):
        for community,row in zip(("HIGH","MEDIUM","LOW"),((7,9,11),(9,11,13),(11,13,15))):
            for category,minutes in zip(("HIGH","MEDIUM","LOW"),row):
                with self.subTest(community=community,category=category):
                    result=fire_response_reference(community,category)
                    self.assertEqual(result["target_seconds"],minutes*60)
                    self.assertEqual(result["reporting_period"],"2023/24")
                    self.assertEqual(result["compliance_status"],"NOT_ASSESSED")

    def test_missing_risk_does_not_invent_target(self):
        for risks in ((None,None),("HIGH",None),(None,"LOW")):
            result=fire_response_reference(*risks)
            self.assertIsNone(result["target_seconds"])
            self.assertEqual(result["classification_status"],"UNASSIGNED")
        with self.assertRaises(ValueError):fire_response_reference("HIGH",4)
