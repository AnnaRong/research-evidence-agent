#!/usr/bin/env python3
"""Server-side MCP pipeline for the research evidence Agent."""

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from mcp_client import MCPError, call_tool, mcp_config


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
URL_RE = re.compile(r"https?://[^\s)<>]+")
MAX_TOOL_OUTPUT_CHARS = 100_000


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def citations(value):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    found = ["https://doi.org/" + doi.rstrip(".,;:}") for doi in DOI_RE.findall(text)]
    found.extend(url.rstrip(".,;:}") for url in URL_RE.findall(text))
    return list(dict.fromkeys(found))


def flatten(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from flatten(child)
    elif isinstance(value, list):
        for child in value:
            yield from flatten(child)


def paper_records(raw):
    result = []
    roots = [raw.get("result", raw)]
    text = raw.get("text", "")
    if text:
        try:
            roots.append(json.loads(text))
        except (TypeError, json.JSONDecodeError):
            pass
    for root in roots:
        for item in flatten(root):
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                try:
                    roots.append(json.loads(item["text"]))
                except (TypeError, json.JSONDecodeError):
                    pass
                continue
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("paper_title") or item.get("name")
            if not title or not isinstance(title, str):
                continue
            doi = item.get("doi") or item.get("DOI")
            url = item.get("url") or item.get("paper_url") or item.get("link")
            abstract = item.get("abstract") or item.get("summary") or item.get("snippet")
            record = {"paper_id": doi or url or "paper-" + uuid.uuid4().hex[:10], "doi": doi, "url": url, "title": title, "abstract": abstract, "source": raw.get("server"), "source_tool": raw.get("tool")}
            result.append(record)
    seen = set()
    deduped = []
    for record in result:
        key = (record.get("doi") or record.get("url") or record["title"].lower()).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _error(exc):
    return {"category": getattr(exc, "category", "mcp_error"), "message": str(exc), "retryable": getattr(exc, "retryable", True), "suggested_actions": ["retry", "switch_tool", "manual_upload"]}


def normalize_tool_result(result, limit=MAX_TOOL_OUTPUT_CHARS):
    """Keep structured tool metadata while bounding context and checkpoint size."""
    normalized = dict(result) if isinstance(result, dict) else {"result": result}
    text = str(normalized.get("text", ""))
    if len(text) > limit:
        normalized["text"] = text[:limit]
        normalized["truncated"] = True
        normalized["original_text_chars"] = len(text)
    normalized.setdefault("truncated", False)
    return normalized


class AgentPipeline:
    def __init__(self, task_store, telemetry, checkpoint):
        self.task_store = task_store
        self.telemetry = telemetry
        self.checkpoint = checkpoint
        self.threads = {}
        self.lock = threading.Lock()

    def start(self, task_id, stage, question, protocol=None, contribution=None):
        with self.lock:
            existing = self.threads.get(task_id)
            if existing and existing.is_alive():
                return {"accepted": True, "status": "running", "task_id": task_id, "stage": stage}
            thread = threading.Thread(target=self.run, args=(task_id, stage, question, protocol or {}, contribution or ""), daemon=True)
            self.threads[task_id] = thread
            thread.start()
        return {"accepted": True, "status": "running", "task_id": task_id, "stage": stage}

    def cancel(self, task_id):
        task = self.task_store.get(task_id)
        if not task:
            raise KeyError("Unknown task: {}".format(task_id))
        state = dict(task.get("state") or {})
        state["cancel_requested"] = True
        state["cancel_requested_at"] = now()
        return self.checkpoint(task_id, status="cancelling", state=state)

    def _cancelled(self, task_id):
        return bool(((self.task_store.get(task_id) or {}).get("state") or {}).get("cancel_requested"))

    def _save(self, task_id, stage, status, state, error=None):
        self.checkpoint(task_id, stage=stage, status=status, state=state, error=error)

    def _call(self, task_id, stage, worker, server, tool, arguments):
        retries = 2
        for attempt in range(retries + 1):
            if self._cancelled(task_id):
                raise MCPError("Task was cancelled by the user", "cancelled", False)
            started = datetime.now(timezone.utc)
            self.telemetry({"task_id": task_id, "event_type": "api_call_started", "stage": stage, "worker": worker, "metadata": {"provider": server, "tool": tool, "attempt": attempt + 1}})
            try:
                result = normalize_tool_result(call_tool(server, tool, arguments))
                elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                self.telemetry({"task_id": task_id, "event_type": "api_call_completed", "stage": stage, "worker": worker, "duration_ms": elapsed, "metadata": {"provider": server, "tool": tool, "attempt": attempt + 1, "estimated_cost_usd": 0, "truncated": result["truncated"]}})
                return result
            except MCPError as exc:
                self.telemetry({"task_id": task_id, "event_type": "worker_failed", "stage": stage, "worker": worker, "status": "failed", "metadata": {"provider": server, "tool": tool, "attempt": attempt + 1, "category": exc.category}})
                if not exc.retryable or attempt == retries:
                    raise
                self.telemetry({"task_id": task_id, "event_type": "worker_retried", "stage": stage, "worker": worker, "status": "running", "metadata": {"provider": server, "tool": tool, "attempt": attempt + 2}})
                time.sleep(0.25 * (2 ** attempt))

    def run(self, task_id, stage, question, protocol, contribution):
        try:
            if stage == "search":
                self.search(task_id, question, protocol)
            elif stage == "screening":
                self.screening(task_id, question, protocol)
            elif stage == "extraction":
                self.extraction(task_id, question, protocol)
            elif stage == "novelty":
                self.novelty(task_id, contribution or question, protocol)
            elif stage == "synthesis":
                self.synthesis(task_id, question, protocol)
            else:
                raise MCPError("Unsupported pipeline stage: {}".format(stage), "unknown", False)
        except MCPError as exc:
            task = self.task_store.get(task_id) or {}
            state = dict(task.get("state") or {})
            state["last_failed_stage"] = stage
            status = "cancelled" if exc.category == "cancelled" else "needs_attention"
            self._save(task_id, stage, status, state, _error(exc))

    def search(self, task_id, question, protocol):
        config = mcp_config()
        if not config["elicit"] and not config["edison"]:
            raise MCPError("No Elicit or Edison MCP credentials are configured", "not_configured", False)
        args = {"query": protocol.get("query") or question, "research_question": question}
        calls = []
        servers = [("elicit", "search_papers")]
        task = self.task_store.get(task_id) or {}
        if (task.get("experiment_group") or "experiment") == "experiment" and config["edison"]:
            servers.append(("edison", "edison_quick_search_agent"))
        with ThreadPoolExecutor(max_workers=len(servers)) as executor:
            futures = {executor.submit(self._call, task_id, "search", "SearchWorker", server, tool, args): (server, tool) for server, tool in servers if (server == "elicit" and config["elicit"]) or (server == "edison" and config["edison"])}
            for future in as_completed(futures):
                server, tool = futures[future]
                raw = future.result()
                calls.append(raw)
        papers = []
        source_citations = []
        for raw in calls:
            papers.extend(paper_records(raw))
            source_citations.extend(citations(raw))
        unique = {}
        for paper in papers:
            unique.setdefault((paper.get("doi") or paper.get("url") or paper["title"]).lower(), paper)
        if not unique:
            raise MCPError("MCP search completed without paper records", "parse_error", False)
        state = dict((self.task_store.get(task_id) or {}).get("state") or {})
        state.update({"candidates": list(unique.values()), "source_citations": list(dict.fromkeys(source_citations)), "mcp_calls": [{"server": row.get("server"), "tool": row.get("tool"), "session_id": row.get("session_id")} for row in calls], "next_gate": "screening_criteria_confirmation"})
        self._save(task_id, "search", "needs_attention", state)
        self.telemetry({"task_id": task_id, "event_type": "worker_completed", "stage": "search", "worker": "SearchWorker", "status": "completed", "metadata": {"candidate_count": len(unique)}})

    def screening(self, task_id, question, protocol):
        task = self.task_store.get(task_id) or {}
        candidates = (task.get("state") or {}).get("candidates", [])
        args = {"research_question": question, "search_query": protocol.get("query") or question, "inclusion_criteria": protocol.get("inclusion_criteria", []), "exclusion_criteria": protocol.get("exclusion_criteria", []), "papers": candidates}
        raw = self._call(task_id, "screening", "ScreeningWorker", "elicit", "create_systematic_review", args)
        state = dict(task.get("state") or {})
        state.update({"screening_raw": raw, "source_citations": list(dict.fromkeys((state.get("source_citations") or []) + citations(raw))), "next_gate": "borderline_screening_confirmation"})
        self._save(task_id, "screening", "needs_attention", state)
        self.telemetry({"task_id": task_id, "event_type": "worker_completed", "stage": "screening", "worker": "ScreeningWorker", "status": "completed"})

    def extraction(self, task_id, question, protocol):
        task = self.task_store.get(task_id) or {}
        state = task.get("state") or {}
        args = {"research_question": question, "papers": state.get("candidates", []), "screening": state.get("screening_raw", {}), "required_fields": ["study_design", "sample_size", "effect_measures", "statistical_methods"]}
        raw = self._call(task_id, "extraction", "ExtractionWorker", "edison", "edison_deep_search_agent", args)
        state = dict(state)
        state.update({"extraction_raw": raw, "source_citations": list(dict.fromkeys((state.get("source_citations") or []) + citations(raw))), "next_gate": "evidence_export_confirmation"})
        self._save(task_id, "extraction", "needs_attention", state)
        self.telemetry({"task_id": task_id, "event_type": "worker_completed", "stage": "extraction", "worker": "ExtractionWorker", "status": "completed"})

    def novelty(self, task_id, contribution, protocol):
        raw = self._call(task_id, "novelty", "NoveltyWorker", "edison", "edison_precedent_search_agent", {"proposed_contribution": contribution, "scope": protocol})
        task = self.task_store.get(task_id) or {}
        state = dict(task.get("state") or {})
        state.update({"novelty_raw": raw, "source_citations": list(dict.fromkeys((state.get("source_citations") or []) + citations(raw)))})
        self._save(task_id, "novelty", "needs_attention", state)
        self.telemetry({"task_id": task_id, "event_type": "worker_completed", "stage": "novelty", "worker": "NoveltyWorker", "status": "completed"})

    def synthesis(self, task_id, question, protocol):
        task = self.task_store.get(task_id) or {}
        state = task.get("state") or {}
        raw = self._call(task_id, "synthesis", "SynthesisWorker", "edison", "edison_deep_search_agent", {"research_question": question, "protocol": protocol, "evidence": state})
        state = dict(state)
        state.update({"synthesis_raw": raw, "source_citations": list(dict.fromkeys((state.get("source_citations") or []) + citations(raw))), "next_gate": "evidence_export_confirmation"})
        self._save(task_id, "synthesis", "needs_attention", state)
        self.telemetry({"task_id": task_id, "event_type": "worker_completed", "stage": "synthesis", "worker": "SynthesisWorker", "status": "completed"})
