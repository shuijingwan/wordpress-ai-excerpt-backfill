from copy import deepcopy
import unittest

from src.eligibility import evaluate_phase1_eligibility


def eligible_post():
    return {
        "post_type": "post",
        "post_status": "publish",
        "language_source": "polylang",
        "language": "zh",
    }


def eligible_analysis():
    return {
        "editor_format": "gutenberg",
        "code_format": "code-block-pro",
        "primary_format": "gutenberg+code-block-pro",
        "matched_rule_ids": [
            "GB_BLOCK_BALANCED",
            "CBP_BLOCK_COMMENT",
            "CBP_BLOCK_CLASS",
            "CBP_SHIKI_STRUCTURE",
        ],
        "risk_level": "low",
        "risk_reasons": [],
    }


class Phase1EligibilityTest(unittest.TestCase):
    def evaluate(self, post_changes=None, analysis_changes=None):
        post = eligible_post()
        analysis = eligible_analysis()
        post.update(post_changes or {})
        analysis.update(analysis_changes or {})
        return evaluate_phase1_eligibility(post, analysis)

    def test_complete_gutenberg_code_block_pro_is_eligible(self):
        result = self.evaluate()
        self.assertEqual({
            "phase": "phase-1",
            "status": "eligible-gutenberg-code-block-pro",
            "eligible": True,
            "exclusion_reasons": [],
        }, result)

    def test_draft_is_excluded(self):
        result = self.evaluate({"post_status": "draft"})
        self.assertIn("EXCLUDE_NOT_PUBLISHED_POST", result["exclusion_reasons"])

    def test_page_is_excluded(self):
        result = self.evaluate({"post_type": "page"})
        self.assertIn("EXCLUDE_NOT_PUBLISHED_POST", result["exclusion_reasons"])

    def test_polylang_english_is_excluded(self):
        result = self.evaluate({"language": "en"})
        self.assertEqual(["EXCLUDE_NOT_POLYLANG_ZH"], result["exclusion_reasons"])

    def test_missing_language_is_language_unknown_only(self):
        post = eligible_post()
        del post["language"]
        result = evaluate_phase1_eligibility(post, eligible_analysis())
        self.assertIn("EXCLUDE_LANGUAGE_UNKNOWN", result["exclusion_reasons"])
        self.assertNotIn("EXCLUDE_NOT_POLYLANG_ZH", result["exclusion_reasons"])

    def test_classic_plain_is_excluded(self):
        result = self.evaluate(analysis_changes={
            "editor_format": "classic",
            "code_format": "none",
            "primary_format": "classic/plain",
            "matched_rule_ids": [],
        })
        self.assertEqual(
            ["EXCLUDE_NOT_GUTENBERG", "EXCLUDE_NO_CODE_BLOCK_PRO"],
            result["exclusion_reasons"],
        )

    def test_gutenberg_plain_is_excluded(self):
        result = self.evaluate(analysis_changes={
            "code_format": "none",
            "primary_format": "gutenberg/plain",
            "matched_rule_ids": ["GB_BLOCK_BALANCED"],
        })
        self.assertIn("EXCLUDE_NO_CODE_BLOCK_PRO", result["exclusion_reasons"])

    def test_gutenberg_syntaxhighlighter_is_excluded(self):
        result = self.evaluate(analysis_changes={
            "code_format": "syntaxhighlighter",
            "primary_format": "gutenberg+syntaxhighlighter",
            "matched_rule_ids": ["GB_BLOCK_BALANCED", "SH_SHORTCODE"],
        })
        self.assertTrue({"EXCLUDE_NO_CODE_BLOCK_PRO", "EXCLUDE_SYNTAXHIGHLIGHTER"} <= set(result["exclusion_reasons"]))

    def test_syntaxhighlighter_and_code_block_pro_mixed_is_excluded(self):
        result = self.evaluate(analysis_changes={
            "code_format": "mixed",
            "primary_format": "mixed",
            "matched_rule_ids": ["CBP_BLOCK_COMMENT", "CBP_BLOCK_CLASS", "CBP_SHIKI_STRUCTURE", "SH_SHORTCODE"],
        })
        self.assertTrue({
            "EXCLUDE_MIXED_CODE_FORMAT",
            "EXCLUDE_NO_CODE_BLOCK_PRO",
            "EXCLUDE_SYNTAXHIGHLIGHTER",
        } <= set(result["exclusion_reasons"]))

    def test_unknown_format_is_excluded(self):
        result = self.evaluate(analysis_changes={
            "editor_format": "unknown", "code_format": "unknown", "primary_format": "unknown",
        })
        self.assertIn("EXCLUDE_FORMAT_UNKNOWN", result["exclusion_reasons"])

    def test_damaged_gutenberg_is_excluded(self):
        result = self.evaluate(analysis_changes={
            "editor_format": "unknown",
            "primary_format": "unknown",
            "matched_rule_ids": ["CBP_BLOCK_COMMENT", "CBP_BLOCK_CLASS", "CBP_SHIKI_STRUCTURE", "GB_BLOCK_DAMAGED"],
            "risk_level": "manual-review",
        })
        self.assertTrue({
            "EXCLUDE_DAMAGED_STRUCTURE", "EXCLUDE_FORMAT_UNKNOWN", "EXCLUDE_MANUAL_REVIEW",
        } <= set(result["exclusion_reasons"]))

    def test_manual_review_is_excluded(self):
        result = self.evaluate(analysis_changes={"risk_level": "manual-review"})
        self.assertEqual(["EXCLUDE_MANUAL_REVIEW"], result["exclusion_reasons"])

    def test_missing_shiki_structure_is_damaged_code_block_pro(self):
        result = self.evaluate(analysis_changes={
            "matched_rule_ids": ["GB_BLOCK_BALANCED", "CBP_BLOCK_COMMENT", "CBP_BLOCK_CLASS"],
        })
        self.assertIn("EXCLUDE_CODE_BLOCK_PRO_DAMAGED", result["exclusion_reasons"])

    def test_shiki_weak_structure_does_not_confirm_code_block_pro(self):
        result = self.evaluate(analysis_changes={
            "code_format": "unknown",
            "primary_format": "unknown",
            "matched_rule_ids": ["CBP_PARTIAL_STRUCTURE"],
        })
        self.assertIn("EXCLUDE_NO_CODE_BLOCK_PRO", result["exclusion_reasons"])

    def test_medium_risk_can_be_structurally_eligible(self):
        result = self.evaluate(analysis_changes={
            "risk_level": "medium",
            "risk_reasons": ["RISK_IFRAME_OR_EMBED"],
        })
        self.assertTrue(result["eligible"])

    def test_exclusion_reasons_are_unique_and_sorted(self):
        result = self.evaluate(analysis_changes={
            "editor_format": "mixed",
            "code_format": "mixed",
            "primary_format": "mixed",
            "matched_rule_ids": ["SH_DAMAGED", "SH_DAMAGED", "SC_UNCLOSED"],
            "risk_level": "manual-review",
        })
        reasons = result["exclusion_reasons"]
        self.assertEqual(sorted(set(reasons)), reasons)

    def test_inputs_are_not_modified(self):
        post = eligible_post()
        analysis = eligible_analysis()
        post_before = deepcopy(post)
        analysis_before = deepcopy(analysis)
        evaluate_phase1_eligibility(post, analysis)
        self.assertEqual(post_before, post)
        self.assertEqual(analysis_before, analysis)

    def test_non_dict_inputs_raise_type_error(self):
        with self.assertRaisesRegex(TypeError, "post must be a dict"):
            evaluate_phase1_eligibility([], eligible_analysis())
        with self.assertRaisesRegex(TypeError, "analysis must be a dict"):
            evaluate_phase1_eligibility(eligible_post(), [])

    def test_invalid_matched_rule_ids_type_raises(self):
        analysis = eligible_analysis()
        analysis["matched_rule_ids"] = "CBP_BLOCK_COMMENT"
        with self.assertRaisesRegex(TypeError, "matched_rule_ids"):
            evaluate_phase1_eligibility(eligible_post(), analysis)

    def test_missing_risk_level_is_not_eligible(self):
        analysis = eligible_analysis()
        del analysis["risk_level"]
        result = evaluate_phase1_eligibility(eligible_post(), analysis)
        self.assertFalse(result["eligible"])
        self.assertIn("EXCLUDE_MANUAL_REVIEW", result["exclusion_reasons"])


if __name__ == "__main__":
    unittest.main()
