import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))

from server import TaskStore, ab_testing_summary, api_call_telemetry, bundle_bytes, save_analytics_config, satisfaction_feedback, telemetry_event


class ResilienceTest(unittest.TestCase):
    def test_task_checkpoint_resume_failure_retry_skip_and_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = TaskStore(Path(temporary) / "state.sqlite3")
            created = store.create(task_id="task-1", session_id="elicit-session", stage="search", state={"papers": []})
            self.assertEqual(created["session_id"], "elicit-session")
            saved = store.checkpoint("task-1", stage="screening", state={"papers": ["p1"], "cursor": 4})
            self.assertEqual(saved["state"]["cursor"], 4)
            reopened = TaskStore(Path(temporary) / "state.sqlite3")
            self.assertEqual(reopened.get("task-1")["sessionId"], "elicit-session")
            self.assertEqual(reopened.get("task-1")["state"]["cursor"], 4)
            failed = store.failure("task-1", {"category": "rate_limit", "message": "429"})
            self.assertEqual(failed["status"], "needs_attention")
            self.assertEqual(failed["error"]["category"], "rate_limit")
            retried = store.retry("task-1", tool="edison_quick_search_agent")
            self.assertEqual(retried["status"], "running")
            self.assertEqual(retried["state"]["retry_count"], 1)
            skipped = store.skip("task-1", "user_requested_skip")
            self.assertEqual(skipped["status"], "running")
            self.assertIn("screening", skipped["state"]["skipped_stages"])
            gate = store.confirm_gate("task-1", "screening_criteria_confirmation", "user")
            self.assertEqual(gate["status"], "confirmed")
            self.assertEqual(next(row for row in store.gates("task-1") if row["gate_name"] == "screening_criteria_confirmation")["status"], "confirmed")

    def test_cost_near_limit_warning_and_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            analytics = Path(temporary)
            save_analytics_config({"task_cost_limit_usd": 5, "near_limit_ratio": 0.8}, analytics)
            result = api_call_telemetry({"task_id": "task-2", "provider": "elicit", "tool": "search_papers", "estimated_cost_usd": 4.1}, analytics)
            self.assertTrue(result["allowed"])
            self.assertTrue(result["near_limit"])
            self.assertIn("预算", result["warning"])

    def test_export_bundle_contains_source_manifest(self):
        import zipfile
        from io import BytesIO

        with zipfile.ZipFile(BytesIO(bundle_bytes())) as archive:
            names = set(archive.namelist())
            self.assertIn("sources.json", names)
            self.assertIn("来源", archive.read("evidence-table.csv").decode("utf-8"))
            self.assertIn("Sources", archive.read("novelty-report.md").decode("utf-8"))

    def test_ab_summary_compares_fixed_task_groups_and_interview_nps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "state.sqlite3")
            store.create(task_id="control-task", experiment_group="control")
            store.create(task_id="experiment-task", experiment_group="experiment")
            analytics = root / "analytics"
            feedback = root / "feedback"
            for task_id, duration in (("control-task", 1000), ("experiment-task", 2000)):
                telemetry_event({"task_id": task_id, "event_type": "task_started"}, analytics)
                telemetry_event({"task_id": task_id, "event_type": "intervention_opportunity", "intervention_type": "extraction", "total_units": 10}, analytics)
                telemetry_event({"task_id": task_id, "event_type": "manual_intervention", "intervention_type": "extraction", "target_id": task_id + ":sample_size"}, analytics)
                telemetry_event({"task_id": task_id, "event_type": "task_exported"}, analytics)
                satisfaction_feedback({"task_id": task_id, "score": 4, "nps_score": 9 if task_id == "experiment-task" else 6, "most_useful_stage": "extraction", "improvement_stage": "screening"}, analytics)
            summary = ab_testing_summary(analytics, feedback, store)
            self.assertEqual(summary["groups"]["control"]["assigned_tasks"], 1)
            self.assertEqual(summary["groups"]["experiment"]["assigned_tasks"], 1)
            self.assertEqual(summary["groups"]["control"]["nps"], -100.0)
            self.assertEqual(summary["groups"]["experiment"]["nps"], 100.0)


if __name__ == "__main__":
    unittest.main()
