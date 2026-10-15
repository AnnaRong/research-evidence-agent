import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "evidence_agent.py"
SPEC = importlib.util.spec_from_file_location("evidence_agent", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EvidenceAgentTest(unittest.TestCase):
    def test_project_dedupe_report_and_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "review"
            MODULE.cmd_init(
                SimpleNamespace(
                    project=str(project),
                    question="Do compound climate risks affect ecosystem boundaries?",
                    force=False,
                )
            )
            MODULE.cmd_mcp_log(
                SimpleNamespace(
                    project=str(project),
                    server="elicit",
                    tool="search_papers",
                    status="completed",
                    task_id=None,
                    session_id="session-1",
                    input_summary="compound heat and drought at ecosystem boundaries",
                    output_artifact="mcp/raw/elicit-search.json",
                    warning=None,
                )
            )
            calls = MODULE.read_jsonl(project / "mcp/calls.jsonl")
            self.assertEqual("search_papers", calls[0]["tool"])
            protocol = json.loads((project / "protocol.json").read_text(encoding="utf-8"))
            protocol["human_confirmed"] = True
            (project / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")

            records = [
                {
                    "record_id": "openalex:W1",
                    "doi": "10.1000/example",
                    "title": "Compound climate risks at ecosystem boundaries",
                    "publication_year": 2025,
                    "authors": ["A. Researcher"],
                    "type": "article",
                    "cited_by_count": 5,
                },
                {
                    "record_id": "crossref:1",
                    "doi": "https://doi.org/10.1000/EXAMPLE",
                    "title": "Compound climate risks at ecosystem boundaries",
                    "publication_year": 2025,
                    "authors": ["A. Researcher"],
                    "type": "journal-article",
                    "cited_by_count": 1,
                },
            ]
            MODULE.write_jsonl(project / "corpus/records.jsonl", records)
            MODULE.cmd_dedupe(SimpleNamespace(project=str(project), threshold=0.92))
            deduped = MODULE.read_jsonl(project / "corpus/deduped.jsonl")
            self.assertEqual(1, len(deduped))
            self.assertEqual(["crossref:1"], deduped[0]["duplicate_of"])

            decision = {
                "paper_id": "openalex:W1",
                "stage": "full_text",
                "decision": "include",
                "reason": "Matches the confirmed protocol",
                "reviewer": "human",
                "human_confirmed": True,
                "timestamp": MODULE.utc_now(),
            }
            evidence = {
                "paper_id": "openalex:W1",
                "variables": ["temperature", "drought"],
                "methods": ["spatial overlay"],
                "findings": ["Compound exposure was reported"],
                "source_spans": [
                    {
                        "location": "Results, p. 5",
                        "access_level": "full_text",
                        "quote_or_paraphrase": "Compound exposure increased.",
                    }
                ],
                "human_verified": True,
            }
            claim = {
                "claim_id": "N001",
                "proposed_contribution": "Add socioeconomic exposure to the compound-risk model",
                "verdict": "partial_precedent",
                "supporting_paper_ids": ["openalex:W1"],
                "contradicting_paper_ids": [],
                "search_queries": ["compound climate ecosystem socioeconomic exposure"],
                "scope_limit": "Test corpus",
                "human_confirmed": True,
            }
            MODULE.write_jsonl(project / "screening/decisions.jsonl", [decision])
            MODULE.write_jsonl(project / "evidence/evidence.jsonl", [evidence])
            MODULE.write_jsonl(project / "novelty/claims.jsonl", [claim])
            errors, warnings = MODULE.validate_project(project)
            self.assertEqual([], errors)
            self.assertEqual([], warnings)

            MODULE.cmd_report(SimpleNamespace(project=str(project)))
            self.assertTrue((project / "reports/evidence-table.csv").exists())
            self.assertIn(
                "N001",
                (project / "reports/novelty-analysis.md").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
