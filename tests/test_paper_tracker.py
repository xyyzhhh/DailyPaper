import unittest
from datetime import date

import paper_tracker


class PaperTrackerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preferences = paper_tracker.load_preferences()

    def test_source_priority_follows_custom_order(self):
        security_paper = {"venue": "USENIX Security Symposium", "externalIds": {}}
        ai_paper = {"venue": "NeurIPS", "externalIds": {}}
        journal_paper = {"venue": "Medical Image Analysis", "externalIds": {}}
        arxiv_paper = {"venue": "arXiv", "externalIds": {"ArXiv": "2608.00001"}}

        self.assertGreater(
            paper_tracker.source_priority(security_paper, self.preferences)[0],
            paper_tracker.source_priority(ai_paper, self.preferences)[0],
        )
        self.assertGreater(
            paper_tracker.source_priority(ai_paper, self.preferences)[0],
            paper_tracker.source_priority(journal_paper, self.preferences)[0],
        )
        self.assertGreater(
            paper_tracker.source_priority(journal_paper, self.preferences)[0],
            paper_tracker.source_priority(arxiv_paper, self.preferences)[0],
        )

    def test_recent_filter_requires_window_or_current_year(self):
        earliest_date = date(2026, 7, 12)

        self.assertTrue(
            paper_tracker.is_recent_enough(
                {"publicationDate": "2026-08-10", "year": 2026}, earliest_date
            )
        )
        self.assertFalse(
            paper_tracker.is_recent_enough(
                {"publicationDate": "2026-06-01", "year": 2026}, earliest_date
            )
        )
        self.assertTrue(
            paper_tracker.is_recent_enough({"year": 2026}, earliest_date)
        )


if __name__ == "__main__":
    unittest.main()
