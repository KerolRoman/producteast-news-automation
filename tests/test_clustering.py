import unittest
from unittest.mock import patch, Mock
from cluster_stories import same_event_score, cluster_items, resolve_publisher_url, hostname, publisher_domain, resolve_stories


class ClusteringRegressionTests(unittest.TestCase):
    def score(self, a, b):
        return same_event_score({"title": a}, {"title": b})[0]

    def test_stampede_and_crush_same_event(self):
        a = "19 dead, 25 hurt in stampede at India religious gathering"
        b = "At least 17 dead in crush at Hindu festival event in India"
        self.assertGreaterEqual(self.score(a, b), 0.80)

    def test_solar_exports_not_same_as_ai_exports(self):
        a = "China moves to curb exports of advanced solar manufacturing technology"
        b = "China Moves to Restrict Exports of 23 AI Technologies; Advanced Models and AI Agents in Focus"
        self.assertLess(self.score(a, b), 0.80)

    def test_two_ai_export_control_reports_can_group(self):
        a = "China Moves to Restrict Exports of 23 AI Technologies; Advanced Models and AI Agents in Focus"
        b = "China says it will curb tech exports, including AI, citing national security risks"
        self.assertGreaterEqual(self.score(a, b), 0.80)

    def test_taiwan_ai_server_and_celebrity_gym_do_not_group(self):
        a = "Taiwan targets illicit AI server exports to China with five-year prison terms"
        b = "Mark Wahlberg wants to take on the longevity-startup crowd with an affordable gym concept"
        self.assertLess(self.score(a, b), 0.80)

    def test_taiwan_export_rules_not_same_as_china_ai_export_policy(self):
        a = "Taiwan targets illicit AI server exports to China with five-year prison terms"
        b = "China Moves to Restrict Exports of 23 AI Technologies; Advanced Models and AI Agents in Focus"
        self.assertLess(self.score(a, b), 0.80)

    def test_china_ai_controls_not_same_as_software_market_consolidation(self):
        a = "China Proposes New Export Controls on AI Models and Chip Technologies Amid US Rivalry"
        b = "Software's new consolidator gets big vote of confidence from Berkshire"
        self.assertLess(self.score(a, b), 0.80)

    def test_no_transitive_chaining(self):
        # A and B share a concrete topic; B and C share a broad phrase; A and C do not.
        items = [
            {"title": "China solar manufacturing export restrictions", "score": 3},
            {"title": "China AI model export restrictions", "score": 2},
            {"title": "China export restrictions for agricultural products", "score": 1},
        ]
        clusters = cluster_items(items)
        # At minimum, the solar and AI subjects must not be merged just because they
        # share the country and generic export-control vocabulary.
        titles = [[s["title"] for s in c["stories"]] for c in clusters]
        self.assertFalse(any(
            any("solar" in t.casefold() for t in group) and
            any("ai model" in t.casefold() for t in group)
            for group in titles
        ))


    def test_taiwan_chip_smuggling_paraphrases_group(self):
        titles = [
            "Taiwan to criminalise AI chip smuggling to China, plans $4B US supply hub - Dealroom",
            "Smuggling Nvidia Chips Into China Is About to Become a Crime in Taiwan - 24/7 Wall St.",
            "Taiwan Wants to Make A.I. Chip Smuggling to China a Crime - www.trendingtopics.eu",
        ]
        clusters = cluster_items([{"title": title, "score": 1} for title in titles])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["story_count"], 3)

    def test_different_tsm_investors_do_not_group(self):
        titles = [
            "Taiwan Semiconductor Manufacturing Company Ltd. $TSM Shares Bought by SteelPeak Wealth LLC - MarketBeat",
            "Taiwan Semiconductor Manufacturing Company Ltd. $TSM Shares Bought by Cyndeo Wealth Partners LLC - MarketBeat",
        ]
        score, reason, _ = same_event_score({"title": titles[0]}, {"title": titles[1]})
        self.assertLess(score, 0.80)
        self.assertIn("different named investors", reason)
        clusters = cluster_items([{"title": title, "score": 1} for title in titles])
        self.assertEqual(len(clusters), 2)

    def test_consent_google_is_not_publisher_resolution(self):
        response = Mock()
        response.url = "https://consent.google.com/ml?continue=https://news.google.com/rss/articles/example"
        response.text = "<html><head><title>Before you continue</title></head></html>"
        with patch("cluster_stories.requests.get", return_value=response):
            url, status = resolve_publisher_url("https://news.google.com/rss/articles/example")
        self.assertEqual(status, "unresolved_google_news")
        self.assertEqual(url, "https://news.google.com/rss/articles/example")

    def test_unresolved_google_url_has_no_publisher_domain(self):
        item = {"url": "https://news.google.com/rss/articles/example", "title": "Story - MarketBeat"}
        with patch("cluster_stories.resolve_publisher_url", return_value=(item["url"], "unresolved_google_news")):
            resolved = resolve_stories([item], {"resolve_google_news_urls": True, "url_resolution_delay_seconds": 0})[0]
        self.assertEqual(resolved["publisher_domain"], "")
        self.assertEqual(resolved["publisher_name_hint"], "MarketBeat")


    def test_rss_publisher_metadata_can_identify_domain_without_article_resolution(self):
        item = {
            "url": "https://news.google.com/rss/articles/example",
            "title": "Example headline - MarketBeat",
            "publisher_name": "MarketBeat",
            "publisher_url": "https://www.marketbeat.com/",
        }
        with patch("cluster_stories.resolve_publisher_url", return_value=(item["url"], "unresolved_google_news")):
            resolved = resolve_stories([item], {"resolve_google_news_urls": True, "url_resolution_delay_seconds": 0})[0]
        self.assertEqual(resolved["publisher_domain"], "marketbeat.com")
        self.assertEqual(resolved["publisher_domain_source"], "google_news_rss_source_metadata")
        self.assertEqual(resolved["url_resolution_status"], "unresolved_google_news")

    def test_direct_url_is_preserved_without_network(self):
        url = "https://www.channelnewsasia.com/asia/example-123"
        from cluster_stories import resolve_publisher_url
        self.assertEqual(resolve_publisher_url(url), (url, "direct_url"))

    def test_hostname_normalization(self):
        self.assertEqual(hostname("https://www.example.com/path"), "example.com")

    def test_publisher_domain_merges_common_subdomains(self):
        self.assertEqual(publisher_domain("https://edition.cnn.com/story"), "cnn.com")
        self.assertEqual(publisher_domain("https://www.bbc.co.uk/news"), "bbc.co.uk")
        self.assertEqual(publisher_domain("https://m.bbc.co.uk/news"), "bbc.co.uk")


if __name__ == "__main__":
    unittest.main()
