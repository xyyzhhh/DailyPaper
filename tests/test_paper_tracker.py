import unittest
from datetime import date
from unittest.mock import Mock, patch

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

    @patch("paper_tracker.request_arxiv")
    @patch("paper_tracker.request_semantic_scholar", return_value=None)
    @patch("paper_tracker.read_list", return_value=[])
    def test_rate_limit_falls_back_to_arxiv_once(
        self, mock_read_list, mock_semantic_scholar, mock_arxiv
    ):
        today = date.today()
        mock_arxiv.return_value = [
            {
                "paperId": "ARXIV:2608.00001",
                "title": "A Watermarking Paper",
                "abstract": "A usable abstract.",
                "authors": [],
                "venue": "arXiv",
                "externalIds": {"ArXiv": "2608.00001"},
                "publicationDate": today.isoformat(),
                "year": today.year,
                "citationCount": 0,
            }
        ]

        preferences = dict(self.preferences)
        preferences["topics"] = [
            {"name": "主题一", "queries": ["query one"]},
            {"name": "主题二", "queries": ["query two"]},
        ]
        with patch("paper_tracker.load_preferences", return_value=preferences):
            papers = paper_tracker.get_paper_recommendations()

        self.assertEqual(1, len(papers))
        self.assertEqual(["主题一", "主题二"], papers[0]["matchedTopics"])
        self.assertEqual(1, mock_semantic_scholar.call_count)
        self.assertEqual(2, mock_arxiv.call_count)

    def test_venue_details_include_abbreviation_and_ccf_rank(self):
        venue_name, abbreviation, ccf_rank = paper_tracker.get_venue_details(
            {"venue": "Conference on Empirical Methods in Natural Language Processing"},
            self.preferences,
        )

        self.assertEqual("Conference on Empirical Methods in Natural Language Processing", venue_name)
        self.assertEqual("EMNLP", abbreviation)
        self.assertEqual("B", ccf_rank)

    def test_unknown_venue_is_marked_not_in_ccf(self):
        _, abbreviation, ccf_rank = paper_tracker.get_venue_details(
            {"venue": "Unlisted Research Venue"}, self.preferences
        )

        self.assertEqual("—", abbreviation)
        self.assertEqual("未收录", ccf_rank)

    def test_daily_digest_limit_is_three(self):
        self.assertEqual(3, self.preferences["max_papers_per_digest"])

    @patch("paper_tracker.requests.get")
    def test_s2_proxy_uses_bearer_token_and_proxy_url(self, mock_get):
        response = Mock(status_code=200)
        response.json.return_value = {"data": []}
        mock_get.return_value = response

        with patch.object(paper_tracker, "S2_PROXY_API_KEY", "test-proxy-token"):
            result = paper_tracker.request_semantic_scholar({"query": "watermarking"})

        self.assertEqual({"data": []}, result)
        request_url = mock_get.call_args.args[0]
        request_headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(
            "https://s2api.ominiai.cn/s2/graph/v1/paper/search", request_url
        )
        self.assertEqual("Bearer test-proxy-token", request_headers["Authorization"])
        self.assertNotIn("x-api-key", request_headers)

    def test_llm_summary_accepts_proxy_plain_text(self):
        self.assertEqual(
            "**问题**：代理返回的阅读笔记",
            paper_tracker.extract_llm_summary("  **问题**：代理返回的阅读笔记  "),
        )

    def test_llm_summary_rejects_html_page(self):
        with self.assertRaisesRegex(RuntimeError, "HTML 页面"):
            paper_tracker.extract_llm_summary(
                "<!doctype html><html><body>4Router</body></html>"
            )

    def test_llm_summary_accepts_openai_json(self):
        response = {"choices": [{"message": {"content": "**方法**：JSON 响应"}}]}

        self.assertEqual("**方法**：JSON 响应", paper_tracker.extract_llm_summary(response))

    def test_llm_summary_accepts_openai_sdk_object(self):
        response = Mock()
        response.choices = [Mock(message=Mock(content="**结果**：SDK 响应"))]

        self.assertEqual("**结果**：SDK 响应", paper_tracker.extract_llm_summary(response))


if __name__ == "__main__":
    unittest.main()
