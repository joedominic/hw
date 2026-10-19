from unittest import TestCase
from resume_app.fact_checker import detect_resume_discrepancies


class FactCheckerTestCase(TestCase):
    def test_clean_draft_has_no_discrepancies(self):
        source = """
        John Doe
        Experience:
        - Delivered enterprise payments platform handling $55MM in annual volume.
        - Reduced false-positive alerts by 40% across fraud surveillance systems.
        - Automated review volume by 60% saving $1.25MM yearly.
        """
        tailored = """
        ## PROFESSIONAL EXPERIENCE
        - Directed payments platform scaling to $55MM in transaction volume.
        - Streamlined fraud surveillance pipeline, reducing false-positive alerts by 40%.
        - Drove 60% automation across watchlist screening, achieving $1.25MM in annual savings.
        """
        report = detect_resume_discrepancies(source, tailored)
        self.assertFalse(report["has_discrepancies"])
        self.assertEqual(report["discrepancies_count"], 0)
        self.assertEqual(len(report["discrepancies"]), 0)

    def test_unverified_percentage_detected(self):
        source = "Managed financial crimes portfolio. Reduced false-positives by 40%."
        tailored = "- Reduced false-positives by 40% and cut model-validation time by 30%."
        report = detect_resume_discrepancies(source, tailored)
        self.assertTrue(report["has_discrepancies"])
        pct_items = [d for d in report["discrepancies"] if d["type"] == "metric" and d["text"] == "30%"]
        self.assertEqual(len(pct_items), 1)
        self.assertIn("30%", pct_items[0]["message"])

    def test_unverified_team_size_detected(self):
        source = "Led distributed engineering teams across architecture and delivery."
        tailored = "- Directed a cross-functional team of 12 engineers across architecture."
        report = detect_resume_discrepancies(source, tailored)
        self.assertTrue(report["has_discrepancies"])
        scope_items = [d for d in report["discrepancies"] if d["type"] == "scope"]
        self.assertTrue(any("12" in d["text"] for d in scope_items))

    def test_unverified_dollar_metric_detected(self):
        source = "Led portfolio with $55MM budget."
        tailored = "- Governed $55MM portfolio and saved $2.5M in infrastructure costs."
        report = detect_resume_discrepancies(source, tailored)
        self.assertTrue(report["has_discrepancies"])
        dollar_items = [d for d in report["discrepancies"] if d["text"] == "$2.5M"]
        self.assertEqual(len(dollar_items), 1)

    def test_hedged_soft_hallucination_detected(self):
        source = "Architected Azure cloud-native platforms with Kafka streaming."
        tailored = "- Evaluated AWS and GCP multi-cloud infrastructure options alongside Azure."
        report = detect_resume_discrepancies(source, tailored)
        self.assertTrue(report["has_discrepancies"])
        tech_items = [d for d in report["discrepancies"] if d["type"] == "technology"]
        tech_names = [d["text"] for d in tech_items]
        self.assertTrue(any("AWS" in t for t in tech_names))

    def test_supplemental_notes_counted_as_ground_truth(self):
        source = "Architected cloud platforms."
        supplemental = "Cut model-validation time by 30% using autonomous AI agents."
        tailored = "- Cut model-validation time by 30% utilizing agentic workflows."
        report = detect_resume_discrepancies(source, tailored, supplemental_text=supplemental)
        self.assertFalse(report["has_discrepancies"])

    def test_sanitize_ats_text_cleans_unicode_hyphens_and_quotes(self):
        from resume_app.parsers import sanitize_ats_text

        raw = (
            "## PROFESSIONAL EXPERIENCE\n"
            "### Wells Fargo - Dallas, TX\n"
            "#### Principal Architect – AI Enablement | 05/2025 - Present\n"
            "- Built enterprise‑scale, cloud‑native platform with latency <250ms.\n"
            "- Championed “Devin” and “GE Genius” iOS app.\n"
            "• **Governing a $55MM portfolio**, delivered CDD screening platform >10 years ahead.\n"
        )
        cleaned = sanitize_ats_text(raw)

        # 1. No U+2011 non-breaking hyphen
        self.assertNotIn("\u2011", cleaned)
        self.assertIn("enterprise-scale", cleaned)
        self.assertIn("cloud-native", cleaned)

        # 2. No curly double quotes
        self.assertNotIn("“", cleaned)
        self.assertNotIn("”", cleaned)
        self.assertIn("'Devin'", cleaned)
        self.assertIn("'GE Genius'", cleaned)

        # 3. No angle brackets
        self.assertNotIn("<", cleaned)
        self.assertNotIn(">", cleaned)
        self.assertIn("under 250ms", cleaned)
        self.assertIn("over 10 years", cleaned)

        # 4. Bullet point normalization and bold stripping
        self.assertNotIn("**Governing", cleaned)
        self.assertIn("- Governing a $55MM portfolio", cleaned)

