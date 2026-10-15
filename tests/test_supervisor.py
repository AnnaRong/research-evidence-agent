import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "supervisor.py"
SPEC = importlib.util.spec_from_file_location("supervisor", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def artifact(worker, result, source="10.1000/example", task_id=None):
    return {
        "task_id": task_id or worker.lower() + "-task-1",
        "worker": worker,
        "status": "completed",
        "source_citations": [{"doi": source, "url": "https://doi.org/" + source}],
        "mcp_calls": [{"server": "elicit", "tool": "test", "session_id": "s-1"}],
        "result": result,
    }


class SupervisorTest(unittest.TestCase):
    def call(self, function, **kwargs):
        with redirect_stdout(io.StringIO()):
            return function(SimpleNamespace(**kwargs))

    def init_running(self, project):
        self.call(MODULE.workflow_init, project=str(project), question="Question", force=False)
        self.call(MODULE.workflow_confirm, project=str(project), gate="protocol", by="human", note="")
        self.call(MODULE.workflow_next, project=str(project))

    def submit(self, project, name, payload):
        path = project / (name + ".json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.call(MODULE.workflow_complete, project=str(project), artifact=str(path))
        return path

    def test_pipeline_gates_and_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "review"
            self.call(MODULE.workflow_init, project=str(project), question="How does compound climate risk affect ecosystems?", force=False)
            state = json.loads((project / "workflow/state.json").read_text(encoding="utf-8"))
            self.assertEqual("awaiting_confirmation", state["status"])
            self.assertEqual("protocol", state["gate"])

            self.call(MODULE.workflow_confirm, project=str(project), gate="protocol", by="human", note="approved")
            self.call(MODULE.workflow_next, project=str(project))
            running = json.loads((project / "workflow/state.json").read_text(encoding="utf-8"))
            self.assertEqual("running", running["stages"][0]["status"])
            search_artifact = self.submit(
                project,
                "search",
                artifact("SearchWorker", {"candidates": [{"paper_id": "p1", "title": "A", "abstract": "B"}]}),
            )
            state = json.loads((project / "workflow/state.json").read_text(encoding="utf-8"))
            self.assertEqual("post_search", state["gate"])
            self.assertEqual(["p1"], state["global_state"]["candidate_paper_ids"])
            self.assertEqual(
                {"task_id", "stage", "input", "output", "source_citations", "timestamp"},
                set(state["shared_state"]["search"]),
            )

            self.call(MODULE.workflow_confirm, project=str(project), gate="post_search", by="human", note="candidate set accepted")
            self.call(MODULE.workflow_next, project=str(project))
            self.submit(
                project,
                "screening",
                artifact(
                    "ScreeningWorker",
                    {
                        "decisions": [
                            {
                                "paper_id": "p1",
                                "stage": "full_text",
                                "decision": "include",
                                "reason": "Meets eligibility criteria",
                            }
                        ],
                        "prisma": {"identified": 1, "included": 1},
                    },
                ),
            )
            state = json.loads((project / "workflow/state.json").read_text(encoding="utf-8"))
            self.assertEqual("post_screening", state["gate"])
            search_input = state["shared_state"]["screening"]["input"]["search"]
            self.assertEqual(state["shared_state"]["search"]["task_id"], search_input["task_id"])
            self.assertEqual(str(search_artifact.resolve()), search_input["artifact"])
            self.assertEqual(64, len(search_input["artifact_sha256"]))

            self.call(MODULE.workflow_confirm, project=str(project), gate="post_screening", by="human", note="decisions accepted")
            self.call(MODULE.workflow_next, project=str(project))
            self.submit(
                project,
                "extraction",
                artifact(
                    "ExtractionWorker",
                    {
                        "extractions": [
                            {
                                "paper_id": "p1",
                                "study_design": "observational",
                                "sample_size": 100,
                                "effect_measures": ["correlation coefficient"],
                                "statistical_methods": ["linear regression"],
                            }
                        ]
                    },
                ),
            )

            self.call(MODULE.workflow_next, project=str(project))
            self.submit(project, "novelty", artifact("NoveltyWorker", {"claims": [{"claim_id": "n1"}]}))
            state = json.loads((project / "workflow/state.json").read_text(encoding="utf-8"))
            self.assertEqual("pre_synthesis", state["gate"])

            self.call(MODULE.workflow_confirm, project=str(project), gate="pre_synthesis", by="human", note="ready")
            self.call(MODULE.workflow_next, project=str(project))
            self.submit(
                project,
                "synthesis",
                artifact("SynthesisWorker", {"evidence_table": [], "method_matrix": [], "narrative": "Synthesis"}),
            )
            state = json.loads((project / "workflow/state.json").read_text(encoding="utf-8"))
            self.assertEqual("completed", state["status"])
            self.assertTrue(state["global_state"]["synthesis_artifact"])
            self.assertEqual({"search", "screening", "extraction", "novelty"}, set(state["shared_state"]["synthesis"]["input"]))

    def test_search_quality_failure_retries_then_requests_intervention(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "review"
            self.init_running(project)

            self.submit(project, "search-1", artifact("SearchWorker", {"candidates": []}, task_id="search-1"))
            state = json.loads((project / "workflow/state.json").read_text(encoding="utf-8"))
            self.assertEqual("needs_retry", state["status"])
            self.assertEqual("failed_quality_gate", state["stages"][0]["status"])

            self.call(MODULE.workflow_next, project=str(project))
            self.submit(project, "search-2", artifact("SearchWorker", {"candidates": []}, task_id="search-2"))
            state = json.loads((project / "workflow/state.json").read_text(encoding="utf-8"))
            self.assertEqual("awaiting_confirmation", state["status"])
            self.assertEqual("quality_intervention", state["gate"])
            self.assertEqual(2, len(state["quality_gates"]))

            self.call(MODULE.workflow_confirm, project=str(project), gate="quality_intervention", by="human", note="broaden query")
            self.call(MODULE.workflow_next, project=str(project))
            state = json.loads((project / "workflow/state.json").read_text(encoding="utf-8"))
            self.assertEqual("search", state["current_stage"])
            self.assertEqual("running", state["stages"][0]["status"])
            self.assertEqual(3, state["stages"][0]["attempts"])

    def test_screening_ratio_outside_policy_fails(self):
        state = {"quality_policy": MODULE.DEFAULT_QUALITY_POLICY, "global_state": {"candidate_paper_ids": ["p{}".format(i) for i in range(5)]}}
        result = {"decisions": [{"paper_id": "p{}".format(i), "stage": "full_text", "decision": "include"} for i in range(5)]}
        quality = MODULE.evaluate_quality_gate(state, "screening", result)
        self.assertFalse(quality["passed"])
        self.assertEqual(1.0, quality["details"]["inclusion_ratio"])

    def test_extraction_missing_rate_at_twenty_percent_fails(self):
        state = {"quality_policy": MODULE.DEFAULT_QUALITY_POLICY, "global_state": {}}
        complete = {
            "study_design": "observational",
            "sample_size": 100,
            "effect_measures": ["odds ratio"],
            "statistical_methods": ["regression"],
        }
        rows = [dict(complete, paper_id="p{}".format(i)) for i in range(5)]
        for index, field in enumerate(MODULE.DEFAULT_QUALITY_POLICY["extraction_critical_fields"]):
            rows[index][field] = None
        quality = MODULE.evaluate_quality_gate(state, "extraction", {"extractions": rows})
        self.assertFalse(quality["passed"])
        self.assertEqual(0.20, quality["details"]["missing_rate"])

    def test_citation_free_worker_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "review"
            self.init_running(project)
            bad = project / "bad.json"
            bad.write_text(json.dumps({"task_id": "search-bad", "worker": "SearchWorker", "status": "completed", "source_citations": [], "mcp_calls": [{"server": "elicit", "tool": "search_papers"}], "result": {"candidates": []}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.call(MODULE.workflow_complete, project=str(project), artifact=str(bad))


if __name__ == "__main__":
    unittest.main()
