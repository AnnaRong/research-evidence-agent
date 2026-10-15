import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))

from server import (
    api_call_telemetry,
    confirm_cost_limit,
    extraction_feedback,
    monitoring_summary,
    satisfaction_feedback,
    save_analytics_config,
    telemetry_event,
)


class MonitoringTest(unittest.TestCase):
    def test_metrics_are_derived_from_events_and_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analytics_dir = root / "analytics"
            feedback_dir = root / "feedback"
            task_id = "task-1"

            telemetry_event({"task_id": task_id, "event_type": "task_started", "status": "running"}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "stage_completed", "stage": "search", "duration_ms": 1200}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "stage_completed", "stage": "screening", "duration_ms": 2400}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "manual_intervention", "intervention_type": "screening", "target_id": "paper-1", "total_units": 5}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "manual_intervention", "intervention_type": "extraction", "target_id": "paper-1:sample_size", "total_units": 40}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "manual_intervention", "intervention_type": "novelty", "target_id": "precedent-1", "total_units": 5}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "output_exported", "metadata": {"cited_items": 8, "total_items": 10}}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "worker_completed", "worker": "SearchWorker"}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "worker_failed", "worker": "ExtractionWorker"}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "worker_retried", "worker": "ExtractionWorker"}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "worker_completed", "worker": "ExtractionWorker"}, analytics_dir)
            extraction_feedback({"paper_id": "paper-1", "field": "sample_size", "source_text": "Paragraph", "original_value": "100", "corrected_value": "120", "total_cells": 10}, feedback_dir)
            satisfaction_feedback({"task_id": task_id, "score": 4, "comment": "Useful"}, analytics_dir)
            telemetry_event({"task_id": task_id, "event_type": "task_exported", "stage": "synthesis", "status": "completed"}, analytics_dir)
            telemetry_event({"task_id": "task-quiet", "event_type": "task_started", "status": "running"}, analytics_dir)
            telemetry_event({"task_id": "task-quiet", "event_type": "intervention_opportunity", "intervention_type": "screening", "total_units": 5}, analytics_dir)
            telemetry_event({"task_id": "task-quiet", "event_type": "intervention_opportunity", "intervention_type": "extraction", "total_units": 40}, analytics_dir)
            telemetry_event({"task_id": "task-quiet", "event_type": "intervention_opportunity", "intervention_type": "novelty", "total_units": 5}, analytics_dir)
            telemetry_event({"task_id": "task-quiet", "event_type": "task_exported", "stage": "synthesis", "status": "completed"}, analytics_dir)

            summary = monitoring_summary(analytics_dir, feedback_dir)
            metrics = summary["metrics"]
            self.assertEqual(metrics["task_completion_rate"], 1.0)
            self.assertEqual(metrics["stage_durations"]["search"]["average_ms"], 1200)
            self.assertEqual(metrics["manual_intervention_rate"], 3 / 100)
            self.assertEqual(metrics["extraction_accuracy"], 0.9)
            self.assertEqual(metrics["source_traceability_rate"], 0.8)
            self.assertEqual(metrics["satisfaction_average"], 4)
            self.assertEqual(summary["tasks"][0]["status"], "completed")
            extraction = next(row for row in summary["workers"] if row["worker"] == "ExtractionWorker")
            self.assertEqual(extraction["failure_rate"], 0.5)
            self.assertEqual(extraction["retries"], 1)

    def test_cost_limit_pauses_until_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            analytics_dir = Path(temporary)
            save_analytics_config({"task_cost_limit_usd": 1}, analytics_dir)
            first = api_call_telemetry({"task_id": "task-2", "provider": "elicit", "tool": "search_papers", "estimated_cost_usd": 0.6}, analytics_dir)
            blocked = api_call_telemetry({"task_id": "task-2", "provider": "edison", "tool": "edison_deep_search_agent", "estimated_cost_usd": 0.5}, analytics_dir)
            self.assertTrue(first["allowed"])
            self.assertFalse(blocked["allowed"])
            self.assertTrue(blocked["confirmation_required"])

            paused = monitoring_summary(analytics_dir, Path(temporary) / "feedback")
            self.assertEqual(paused["tasks"][0]["status"], "paused_cost_limit")

            confirm_cost_limit({"task_id": "task-2", "confirmed_by": "user"}, analytics_dir)
            resumed = api_call_telemetry({"task_id": "task-2", "provider": "edison", "tool": "edison_deep_search_agent", "estimated_cost_usd": 0.5}, analytics_dir)
            self.assertTrue(resumed["allowed"])
            summary = monitoring_summary(analytics_dir, Path(temporary) / "feedback")
            self.assertEqual(summary["metrics"]["api_calls"], 2)
            self.assertEqual(summary["metrics"]["estimated_api_cost_usd"], 1.1)


if __name__ == "__main__":
    unittest.main()
