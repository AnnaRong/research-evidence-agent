import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "ui"))

import model_agent
import server
from agent_pipeline import AgentPipeline


class ModelAgentTest(unittest.TestCase):
    def test_model_status_without_credentials(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                model_agent.model_status(),
                {"configured": False, "provider": None, "model": None},
            )

    def test_openai_model_status_does_not_expose_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "secret", "OPENAI_MODEL": "test-model"}, clear=True):
            status = model_agent.model_status()
            self.assertEqual(status, {"configured": True, "provider": "openai", "model": "test-model"})
            self.assertNotIn("secret", json.dumps(status))

    def test_recruiting_gateway_contract_is_supported_without_exposing_key(self):
        environment = {
            "LLM_API_KEY": "gateway-secret",
            "LLM_EGRESS_ENABLED": "1",
            "LLM_BASE_URL": "https://gateway.test/v1",
            "LLM_ALLOWED_HOST": "gateway.test",
            "LLM_MODEL": "gateway-model",
        }
        with patch.dict("os.environ", environment, clear=True):
            config = model_agent.model_config()
            status = model_agent.model_status()
        self.assertEqual(config["endpoint"], "chat/completions")
        self.assertEqual(status, {"configured": True, "provider": "openai-compatible", "model": "gateway-model"})
        self.assertNotIn("gateway-secret", json.dumps(status))

    def test_local_openai_compatible_model_is_supported(self):
        with patch.dict("os.environ", {"LOCAL_LLM_BASE_URL": "http://127.0.0.1:11434/v1", "LOCAL_LLM_MODEL": "qwen"}, clear=True):
            self.assertEqual(model_agent.model_status(), {"configured": True, "provider": "local-openai-compatible", "model": "qwen"})

    def test_json_schema_rejects_missing_required_field(self):
        with self.assertRaises(model_agent.ModelError) as raised:
            model_agent.validate_json_schema({}, {"type": "object", "required": ["result"]})
        self.assertEqual(raised.exception.category, "schema_validation")

    def test_model_retry_falls_back_to_next_provider(self):
        configs = [{"provider": "first", "model": "a"}, {"provider": "second", "model": "b"}]
        def invoke(config, *_args, **_kwargs):
            if config["provider"] == "first":
                raise model_agent.ModelError("temporary", "timeout", True)
            return {"text": "ok", "provider": "second", "model": "b", "usage": {"total_tokens": 3}}
        with patch.object(model_agent, "model_configs", return_value=configs), patch.object(model_agent, "_invoke", side_effect=invoke), patch.object(model_agent.time, "sleep"):
            result = model_agent.complete_result("system", "user", retries=1)
        self.assertEqual(result["text"], "ok")
        self.assertEqual(result["fallback_count"], 2)

    def test_protocol_schema_is_model_generated(self):
        response = {
            "framework": "PEO",
            "population": "Alpine ecosystems",
            "exposure": "Compound heat and drought",
            "outcome": "Productivity",
            "query": "alpine AND drought",
            "inclusion_criteria": ["Primary studies"],
            "exclusion_criteria": ["Editorials"],
            "limitations": ["Language limits"],
        }
        wrapped = {"value": response, "provider": "test", "model": "test-model", "usage": {"total_tokens": 1}, "attempts": 1, "fallback_count": 0, "duration_ms": 1}
        with patch.object(model_agent, "complete_json", return_value=wrapped):
            result = model_agent.generate_protocol_with_model("Question", ["evidence"])
        self.assertEqual(result["generated_by"], "llm_supervisor")
        self.assertTrue(result["editable"])
        self.assertEqual(result["source_citations"], [])

    def test_protocol_route_requires_configured_model(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                "http://127.0.0.1:{}/api/protocol".format(httpd.server_port),
                data=json.dumps({"question": "Question"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with patch.object(server, "model_status", return_value={"configured": False, "provider": None, "model": None}):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)
            self.assertEqual(raised.exception.code, 503)
            body = json.loads(raised.exception.read().decode())
            self.assertEqual(body["error"], "model_not_configured")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_public_model_rate_limit(self):
        server.MODEL_REQUESTS.clear()
        with patch.dict("os.environ", {"REA_MODEL_REQUESTS_PER_HOUR": "2"}, clear=False):
            self.assertTrue(server.allow_model_request("client", now=100)[0])
            self.assertTrue(server.allow_model_request("client", now=101)[0])
            self.assertFalse(server.allow_model_request("client", now=102)[0])

    def test_pipeline_search_persists_real_mcp_candidates(self):
        import tempfile
        from server import TaskStore

        with tempfile.TemporaryDirectory() as temporary:
            store = TaskStore(Path(temporary) / "state.sqlite3")
            store.create(task_id="pipeline-task", experiment_group="control")
            events = []
            checkpoints = []
            pipeline = AgentPipeline(store, events.append, lambda task_id, **changes: checkpoints.append((task_id, changes)) or store.checkpoint(task_id, **changes))
            raw = {"server": "elicit", "tool": "search_papers", "session_id": "elicit-session", "result": {"content": [{"type": "text", "text": json.dumps({"papers": [{"title": "A real paper", "doi": "10.1234/real.paper", "abstract": "Evidence"}]})}]}, "text": "A real paper 10.1234/real.paper"}
            with patch.dict("os.environ", {"ELICIT_MCP_TOKEN": "test-token", "EDISON_API_KEY": ""}, clear=False), patch("agent_pipeline.call_tool", return_value=raw), patch("agent_pipeline.mcp_config", return_value={"elicit": True, "edison": False, "elicit_url": "https://elicit.com/api/mcp"}):
                pipeline.search("pipeline-task", "Question", {"query": "question"})
            task = store.get("pipeline-task")
            self.assertEqual(task["status"], "needs_attention")
            self.assertEqual(task["state"]["candidates"][0]["doi"], "10.1234/real.paper")
            self.assertEqual(task["state"]["mcp_calls"][0]["session_id"], "elicit-session")


if __name__ == "__main__":
    unittest.main()
