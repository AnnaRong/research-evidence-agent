import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))

from server import extraction_feedback, feedback_summary, novelty_feedback, screening_feedback


class FeedbackTest(unittest.TestCase):
    def test_screening_feedback_writes_audit_event_and_weekly_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            feedback_dir = Path(temporary)
            result = screening_feedback(
                {
                    "paper_id": "p-1",
                    "title": "Example",
                    "source": "Elicit",
                    "original_decision": "exclude",
                    "new_decision": "include",
                    "reason": "Full text meets eligibility criteria",
                },
                feedback_dir,
            )

            event = json.loads((feedback_dir / "screening.jsonl").read_text(encoding="utf-8"))
            weekly = result["weekly_summary"]
            self.assertEqual(event["paper_id"], "p-1")
            self.assertEqual(weekly["transitions"], {"exclude->include": 1})
            self.assertEqual(weekly["few_shot_examples"][0]["reason"], "Full text meets eligibility criteria")
            self.assertTrue((feedback_dir / "weekly" / "screening-{}.json".format(weekly["week"])).exists())

    def test_extraction_accuracy_boundary_and_unique_cells(self):
        with tempfile.TemporaryDirectory() as temporary:
            feedback_dir = Path(temporary)
            result = None
            for index in range(6):
                result = extraction_feedback(
                    {
                        "paper_id": "p-{}".format(index),
                        "field": "sample_size",
                        "source_text": "Source paragraph",
                        "original_value": "100",
                        "corrected_value": "120",
                        "total_cells": 40,
                    },
                    feedback_dir,
                )
            self.assertEqual(result["accuracy"], 0.85)
            self.assertFalse(result["reextraction_triggered"])

            duplicate = extraction_feedback(
                {
                    "paper_id": "p-0",
                    "field": "sample_size",
                    "source_text": "Source paragraph",
                    "original_value": "120",
                    "corrected_value": "125",
                    "total_cells": 40,
                },
                feedback_dir,
            )
            self.assertEqual(duplicate["corrected_cells"], 6)
            self.assertFalse(duplicate["reextraction_triggered"])

            triggered = extraction_feedback(
                {
                    "paper_id": "p-6",
                    "field": "sample_size",
                    "source_text": "Source paragraph",
                    "original_value": "100",
                    "corrected_value": "130",
                    "total_cells": 40,
                },
                feedback_dir,
            )
            self.assertEqual(triggered["accuracy"], 0.825)
            self.assertTrue(triggered["reextraction_triggered"])
            self.assertEqual(triggered["reextraction_task"]["tool"], "edison_deep_search_agent")
            self.assertEqual(triggered["reextraction_task"]["mode"], "cross_validation")

    def test_novelty_accuracy_threshold_and_empty_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            feedback_dir = Path(temporary)
            empty = feedback_summary(feedback_dir)
            self.assertIsNone(empty["extraction"]["accuracy"])
            self.assertIsNone(empty["novelty"]["historical_accuracy"])
            self.assertEqual(empty["novelty"]["recommended_similarity_threshold"], 0.65)

            novelty_feedback(
                {
                    "item_id": "precedent-1",
                    "similarity": 0.8,
                    "agent_label": "known_precedent",
                    "user_label": "known_precedent",
                },
                feedback_dir,
            )
            result = novelty_feedback(
                {
                    "item_id": "precedent-2",
                    "similarity": 0.6,
                    "agent_label": "known_precedent",
                    "user_label": "new_discovery",
                },
                feedback_dir,
            )
            self.assertEqual(result["historical_accuracy"], 0.5)
            self.assertEqual(result["recommended_similarity_threshold"], 0.7)

            summary = feedback_summary(feedback_dir)
            self.assertEqual(summary["novelty"]["historical_accuracy"], 0.5)
            self.assertEqual(summary["novelty"]["recommended_similarity_threshold"], 0.7)


if __name__ == "__main__":
    unittest.main()
