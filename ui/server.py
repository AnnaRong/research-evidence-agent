#!/usr/bin/env python3
"""Public-capable web server for the Research Evidence Agent."""

import json
import mimetypes
import os
import random
import sqlite3
import statistics
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
FEEDBACK_DIR = Path(os.environ.get("REA_FEEDBACK_DIR", REPO / "feedback"))
ANALYTICS_DIR = Path(os.environ.get("REA_ANALYTICS_DIR", REPO / "analytics"))
STATE_DB = Path(os.environ.get("REA_STATE_DB", REPO / "state" / "research_agent.sqlite3"))
FEEDBACK_LOCK = threading.Lock()
MODEL_RATE_LOCK = threading.Lock()
MODEL_REQUESTS = {}
sys.path.insert(0, str(REPO / "scripts"))
from model_agent import ModelError, agent_reply, generate_protocol_with_model, model_status
from agent_pipeline import AgentPipeline
from mcp_client import mcp_config


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def allow_model_request(client_id, now=None):
    """Bound public model spend with a small per-client rolling-hour limit."""
    limit = max(1, int(os.environ.get("REA_MODEL_REQUESTS_PER_HOUR", "20")))
    now = now or time.time()
    cutoff = now - 3600
    with MODEL_RATE_LOCK:
        recent = [timestamp for timestamp in MODEL_REQUESTS.get(client_id, []) if timestamp >= cutoff]
        if len(recent) >= limit:
            MODEL_REQUESTS[client_id] = recent
            return False, limit
        recent.append(now)
        MODEL_REQUESTS[client_id] = recent
        return True, limit


class TaskStore:
    """Small SQLite-backed checkpoint store for resumable research tasks."""

    GATE_NAMES = (
        "protocol_confirmation",
        "screening_criteria_confirmation",
        "borderline_screening_confirmation",
        "evidence_export_confirmation",
    )

    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("REA_STATE_DB", STATE_DB))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self):
        with FEEDBACK_LOCK, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    provider TEXT,
                    tool TEXT,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    experiment_group TEXT NOT NULL DEFAULT 'control',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    state_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_gates (
                    task_id TEXT NOT NULL,
                    gate_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    confirmed_by TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, gate_name),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            if "experiment_group" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN experiment_group TEXT NOT NULL DEFAULT 'control'")

    @staticmethod
    def _json(value):
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("input_json", "state_json", "error_json"):
            if result.get(key):
                result[key[:-5] if key.endswith("_json") else key] = json.loads(result[key])
            result.pop(key, None)
        result["sessionId"] = result.get("session_id", "")
        return result

    def _event(self, connection, task_id, event_type, payload=None):
        connection.execute(
            "INSERT INTO task_events(event_id, task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, task_id, event_type, self._json(payload), utc_now()),
        )

    def create(self, task_id=None, session_id=None, provider="", tool="", stage="protocol", status="running", input_data=None, state=None, experiment_group=None):
        task_id = task_id or "task-{}".format(uuid.uuid4().hex[:12])
        experiment_group = experiment_group or random.SystemRandom().choice(("control", "experiment"))
        if experiment_group not in {"control", "experiment"}:
            raise ValueError("experiment_group must be control or experiment")
        now = utc_now()
        initial_state = dict(state or {})
        initial_state.setdefault("pipeline", "elicit_only" if experiment_group == "control" else "elicit_edison")
        with FEEDBACK_LOCK, self._connect() as connection:
            existing = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if existing:
                return self._decode(existing)
            connection.execute(
                "INSERT INTO tasks(task_id, session_id, provider, tool, stage, status, experiment_group, input_json, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, session_id or "", provider or "", tool or "", stage, status, experiment_group, self._json(input_data), self._json(initial_state), now, now),
            )
            for gate in self.GATE_NAMES:
                connection.execute(
                    "INSERT INTO task_gates(task_id, gate_name, status, updated_at) VALUES (?, ?, 'pending', ?)",
                    (task_id, gate, now),
                )
            self._event(connection, task_id, "task_created", {"stage": stage, "session_id": session_id, "experiment_group": experiment_group})
            return self._decode(connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())

    def get(self, task_id):
        with FEEDBACK_LOCK, self._connect() as connection:
            return self._decode(connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())

    def checkpoint(self, task_id, **changes):
        allowed = {"session_id", "provider", "tool", "stage", "status"}
        fields, values = [], []
        for key in allowed:
            if key in changes and changes[key] is not None:
                fields.append("{} = ?".format(key))
                values.append(changes[key])
        if "input_data" in changes:
            fields.append("input_json = ?")
            values.append(self._json(changes["input_data"]))
        if "error" in changes:
            fields.append("error_json = ?")
            values.append(self._json(changes["error"]) if changes["error"] is not None else None)
        fields.append("updated_at = ?")
        values.extend([utc_now(), task_id])
        with FEEDBACK_LOCK, self._connect() as connection:
            existing = connection.execute("SELECT state_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not existing:
                raise KeyError("Unknown task: {}".format(task_id))
            if "state" in changes:
                previous_state = json.loads(existing[0] or "{}")
                merged_state = dict(previous_state)
                merged_state.update(changes["state"] or {})
                fields.insert(0, "state_json = ?")
                values.insert(0, self._json(merged_state))
            connection.execute("UPDATE tasks SET {} WHERE task_id = ?".format(", ".join(fields)), values)
            self._event(connection, task_id, "checkpoint", {key: value for key, value in changes.items() if key not in {"input_data", "state", "error"}})
            return self._decode(connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())

    def resume(self, task_id):
        return self.checkpoint(task_id, status="running", error=None)

    def failure(self, task_id, error):
        normalized = normalize_worker_error(error)
        return self.checkpoint(task_id, status="needs_attention", error=normalized)

    def retry(self, task_id, tool=None):
        task = self.get(task_id)
        if not task:
            raise KeyError("Unknown task: {}".format(task_id))
        state = task.get("state") or {}
        state["retry_count"] = int(state.get("retry_count", 0)) + 1
        return self.checkpoint(task_id, status="running", tool=tool, state=state, error=None)

    def skip(self, task_id, reason="user_requested_skip"):
        task = self.get(task_id)
        if not task:
            raise KeyError("Unknown task: {}".format(task_id))
        state = task.get("state") or {}
        skipped = list(state.get("skipped_stages", []))
        if task.get("stage") not in skipped:
            skipped.append(task.get("stage"))
        state["skipped_stages"] = skipped
        state["skip_reason"] = reason
        result = self.checkpoint(task_id, status="running", state=state, error={"category": "skipped", "message": reason, "retryable": False})
        with FEEDBACK_LOCK, self._connect() as connection:
            self._event(connection, task_id, "stage_skipped", {"stage": task.get("stage"), "reason": reason})
        return result

    def gates(self, task_id):
        with FEEDBACK_LOCK, self._connect() as connection:
            rows = connection.execute("SELECT * FROM task_gates WHERE task_id = ? ORDER BY gate_name", (task_id,)).fetchall()
            return [{"task_id": row["task_id"], "gate_name": row["gate_name"], "status": row["status"], "confirmed_by": row["confirmed_by"], "metadata": json.loads(row["metadata_json"]), "updated_at": row["updated_at"]} for row in rows]

    def confirm_gate(self, task_id, gate_name, confirmed_by="user", metadata=None):
        if gate_name not in self.GATE_NAMES:
            raise ValueError("Unknown gate: {}".format(gate_name))
        now = utc_now()
        with FEEDBACK_LOCK, self._connect() as connection:
            if not connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
                raise KeyError("Unknown task: {}".format(task_id))
            connection.execute("UPDATE task_gates SET status = 'confirmed', confirmed_by = ?, metadata_json = ?, updated_at = ? WHERE task_id = ? AND gate_name = ?", (confirmed_by, self._json(metadata), now, task_id, gate_name))
            self._event(connection, task_id, "gate_confirmed", {"gate_name": gate_name, "confirmed_by": confirmed_by, "metadata": metadata or {}})
            return {"task_id": task_id, "gate_name": gate_name, "status": "confirmed", "confirmed_by": confirmed_by, "metadata": metadata or {}, "updated_at": now}

    def recent(self, limit=20):
        with FEEDBACK_LOCK, self._connect() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
            return [self._decode(row) for row in rows]

    def experiment_groups(self):
        with FEEDBACK_LOCK, self._connect() as connection:
            return {row["task_id"]: row["experiment_group"] for row in connection.execute("SELECT task_id, experiment_group FROM tasks").fetchall()}


def normalize_worker_error(error):
    if isinstance(error, dict):
        category = str(error.get("category", "unknown"))
        message = str(error.get("message", "Worker failed"))
        retryable = bool(error.get("retryable", category in {"rate_limit", "timeout"}))
        actions = error.get("suggested_actions") or (["retry", "switch_tool", "manual_upload"] if retryable else ["manual_upload"])
    else:
        message = str(error or "Worker failed")
        lowered = message.lower()
        category = "rate_limit" if "rate" in lowered or "429" in lowered else "timeout" if "timeout" in lowered else "parse_error" if "parse" in lowered or "json" in lowered else "unknown"
        retryable = category in {"rate_limit", "timeout"}
        actions = ["retry", "switch_tool", "manual_upload"] if retryable else ["retry", "manual_upload"]
    labels = {"rate_limit": "API 限流", "timeout": "超时", "parse_error": "解析错误", "unknown": "未知错误", "skipped": "用户跳过"}
    return {"category": category, "label": labels.get(category, category), "message": message, "retryable": retryable, "suggested_actions": actions}


TASK_STORE = TaskStore()


def append_jsonl(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with FEEDBACK_LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def week_key(timestamp=None):
    moment = datetime.fromisoformat((timestamp or utc_now()).replace("Z", "+00:00"))
    year, week, _ = moment.isocalendar()
    return "{}-W{:02d}".format(year, week)


def screening_feedback(payload, feedback_dir=FEEDBACK_DIR):
    required = ("paper_id", "original_decision", "new_decision", "reason")
    missing = [key for key in required if not str(payload.get(key, "")).strip()]
    if missing:
        raise ValueError("Missing screening feedback fields: {}".format(", ".join(missing)))
    event = {
        "event_id": "screening-{}".format(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "timestamp": utc_now(),
        "paper_id": payload["paper_id"],
        "task_id": payload.get("task_id"),
        "original_decision": payload["original_decision"],
        "new_decision": payload["new_decision"],
        "reason": payload["reason"],
        "source": payload.get("source"),
        "title": payload.get("title"),
    }
    append_jsonl(feedback_dir / "screening.jsonl", event)
    events = read_jsonl(feedback_dir / "screening.jsonl")
    current_week = week_key(event["timestamp"])
    weekly = [row for row in events if week_key(row["timestamp"]) == current_week]
    transitions = {}
    for row in weekly:
        key = "{}->{}".format(row["original_decision"], row["new_decision"])
        transitions[key] = transitions.get(key, 0) + 1
    summary = {
        "week": current_week,
        "correction_count": len(weekly),
        "transitions": transitions,
        "few_shot_examples": weekly[-20:],
        "usage": "Review before changing ScreeningWorker prompts or few-shot examples.",
        "generated_at": utc_now(),
    }
    weekly_path = feedback_dir / "weekly" / "screening-{}.json".format(current_week)
    weekly_path.parent.mkdir(parents=True, exist_ok=True)
    weekly_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"recorded": True, "message": "你的修正已记录，将用于改进后续筛选", "weekly_summary": summary}


def extraction_feedback(payload, feedback_dir=FEEDBACK_DIR):
    required = ("paper_id", "field", "source_text", "original_value", "corrected_value")
    missing = [key for key in required if payload.get(key) is None or str(payload.get(key)).strip() == ""]
    if missing:
        raise ValueError("Missing extraction feedback fields: {}".format(", ".join(missing)))
    event = {
        "event_id": "extraction-{}".format(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "timestamp": utc_now(),
        "paper_id": payload["paper_id"],
        "task_id": payload.get("task_id"),
        "field": payload["field"],
        "source_text": payload["source_text"],
        "original_value": payload["original_value"],
        "corrected_value": payload["corrected_value"],
        "total_cells": max(1, int(payload.get("total_cells", 1))),
    }
    append_jsonl(feedback_dir / "extraction.jsonl", event)
    events = read_jsonl(feedback_dir / "extraction.jsonl")
    corrected = {(row["paper_id"], row["field"]) for row in events}
    total_cells = max([row.get("total_cells", 1) for row in events] + [event["total_cells"]])
    accuracy = max(0.0, (total_cells - len(corrected)) / total_cells)
    triggered = accuracy < 0.85
    task = None
    if triggered:
        tasks_path = feedback_dir / "reextract_tasks.jsonl"
        tasks = read_jsonl(tasks_path)
        open_task = next((row for row in reversed(tasks) if row.get("status") == "pending"), None)
        if open_task:
            task = open_task
        else:
            task = {
                "task_id": "reextract-{}".format(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "stage": "extraction",
                "status": "pending",
                "reason": "extraction_accuracy_below_85_percent",
                "accuracy": accuracy,
                "tool": "edison_deep_search_agent",
                "mode": "cross_validation",
                "paper_fields": [{"paper_id": row["paper_id"], "field": row["field"]} for row in events[-50:]],
                "timestamp": utc_now(),
            }
            append_jsonl(tasks_path, task)
    return {
        "recorded": True,
        "accuracy": accuracy,
        "corrected_cells": len(corrected),
        "total_cells": total_cells,
        "reextraction_triggered": triggered,
        "reextraction_task": task,
    }


def novelty_feedback(payload, feedback_dir=FEEDBACK_DIR):
    required = ("item_id", "user_label")
    missing = [key for key in required if not str(payload.get(key, "")).strip()]
    if missing:
        raise ValueError("Missing novelty feedback fields: {}".format(", ".join(missing)))
    event = {
        "event_id": "novelty-{}".format(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "timestamp": utc_now(),
        "item_id": payload["item_id"],
        "task_id": payload.get("task_id"),
        "title": payload.get("title"),
        "similarity": float(payload.get("similarity", 0)),
        "agent_label": payload.get("agent_label", "known_precedent"),
        "user_label": payload["user_label"],
        "affects_accuracy": bool(payload.get("affects_accuracy", True)),
    }
    append_jsonl(feedback_dir / "novelty.jsonl", event)
    events = [row for row in read_jsonl(feedback_dir / "novelty.jsonl") if row.get("affects_accuracy", True)]
    matches = sum(row.get("agent_label") == row.get("user_label") for row in events)
    accuracy = matches / len(events) if events else None
    threshold = novelty_threshold(events)
    return {"recorded": True, "historical_accuracy": accuracy, "feedback_count": len(events), "recommended_similarity_threshold": threshold}


def novelty_threshold(events):
    known = [row["similarity"] for row in events if row.get("user_label") == "known_precedent"]
    discoveries = [row["similarity"] for row in events if row.get("user_label") == "new_discovery"]
    if known and discoveries:
        return (min(known) + max(discoveries)) / 2
    if known:
        return min(known)
    if discoveries:
        return max(discoveries)
    return 0.65


def feedback_summary(feedback_dir=FEEDBACK_DIR):
    screening = read_jsonl(feedback_dir / "screening.jsonl")
    extraction = read_jsonl(feedback_dir / "extraction.jsonl")
    novelty = [row for row in read_jsonl(feedback_dir / "novelty.jsonl") if row.get("affects_accuracy", True)]
    corrected = {(row["paper_id"], row["field"]) for row in extraction}
    total_cells = max([row.get("total_cells", 0) for row in extraction] + [0])
    extraction_accuracy = (total_cells - len(corrected)) / total_cells if total_cells else None
    matches = sum(row.get("agent_label") == row.get("user_label") for row in novelty)
    novelty_accuracy = matches / len(novelty) if novelty else None
    current_week = week_key()
    reextract_tasks = read_jsonl(feedback_dir / "reextract_tasks.jsonl")
    return {
        "screening": {"weekly_corrections": sum(week_key(row["timestamp"]) == current_week for row in screening), "total_corrections": len(screening)},
        "extraction": {"accuracy": extraction_accuracy, "corrected_cells": len(corrected), "total_cells": total_cells, "reextraction_pending": any(row.get("status") == "pending" for row in reextract_tasks)},
        "novelty": {"historical_accuracy": novelty_accuracy, "feedback_count": len(novelty), "recommended_similarity_threshold": novelty_threshold(novelty)},
    }


def analytics_config(analytics_dir=ANALYTICS_DIR):
    path = analytics_dir / "config.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "task_cost_limit_usd": float(os.environ.get("REA_TASK_COST_LIMIT_USD", "5")),
        "near_limit_ratio": float(os.environ.get("REA_NEAR_LIMIT_RATIO", "0.8")),
    }


def save_analytics_config(payload, analytics_dir=ANALYTICS_DIR):
    limit = float(payload.get("task_cost_limit_usd", 0))
    if limit <= 0:
        raise ValueError("task_cost_limit_usd must be greater than zero")
    ratio = float(payload.get("near_limit_ratio", 0.8))
    if not 0 < ratio < 1:
        raise ValueError("near_limit_ratio must be between 0 and 1")
    config = {"task_cost_limit_usd": round(limit, 4), "near_limit_ratio": round(ratio, 4), "updated_at": utc_now()}
    path = analytics_dir / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with FEEDBACK_LOCK:
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config


def telemetry_event(payload, analytics_dir=ANALYTICS_DIR):
    task_id = str(payload.get("task_id", "")).strip()
    event_type = str(payload.get("event_type", "")).strip()
    if not task_id or not event_type:
        raise ValueError("task_id and event_type are required")
    experiment_group = payload.get("experiment_group")
    if not experiment_group:
        try:
            experiment_group = TASK_STORE.experiment_groups().get(task_id)
        except Exception:
            experiment_group = None
    event = {
        "event_id": "event-{}".format(uuid.uuid4().hex),
        "timestamp": utc_now(),
        "task_id": task_id,
        "event_type": event_type,
        "stage": payload.get("stage"),
        "worker": payload.get("worker"),
        "experiment_group": experiment_group,
        "status": payload.get("status"),
        "duration_ms": max(0, int(payload.get("duration_ms", 0) or 0)),
        "target_id": payload.get("target_id"),
        "intervention_type": payload.get("intervention_type"),
        "total_units": max(0, int(payload.get("total_units", 0) or 0)),
        "metadata": payload.get("metadata") or {},
    }
    append_jsonl(analytics_dir / "events.jsonl", event)
    return {"recorded": True, "event": event}


PIPELINE = AgentPipeline(TASK_STORE, telemetry_event, TASK_STORE.checkpoint)


def satisfaction_feedback(payload, analytics_dir=ANALYTICS_DIR):
    score = int(payload.get("score", 0))
    if score not in range(1, 6):
        raise ValueError("score must be between 1 and 5")
    raw_nps = payload.get("nps_score")
    nps_score = int(raw_nps) if raw_nps not in (None, "") else None
    if nps_score is not None and nps_score not in range(0, 11):
        raise ValueError("nps_score must be between 0 and 10")
    most_useful_stage = str(payload.get("most_useful_stage", "未填写")).strip() or "未填写"
    improvement_stage = str(payload.get("improvement_stage", "未填写")).strip() or "未填写"
    return telemetry_event(
        {
            "task_id": payload.get("task_id"),
            "event_type": "satisfaction_submitted",
            "metadata": {
                "score": score,
                "nps_score": nps_score,
                "most_useful_stage": most_useful_stage,
                "improvement_stage": improvement_stage,
                "comment": str(payload.get("comment", "")).strip(),
            },
        },
        analytics_dir,
    )


def api_call_telemetry(payload, analytics_dir=ANALYTICS_DIR):
    task_id = str(payload.get("task_id", "")).strip()
    provider = str(payload.get("provider", "")).strip().lower()
    tool = str(payload.get("tool", "")).strip()
    cost = max(0.0, float(payload.get("estimated_cost_usd", 0) or 0))
    if not task_id or provider not in {"elicit", "edison"} or not tool:
        raise ValueError("task_id, provider (elicit/edison), and tool are required")
    events = read_jsonl(analytics_dir / "events.jsonl")
    task_events = [row for row in events if row.get("task_id") == task_id]
    confirmed = any(row.get("event_type") == "cost_limit_confirmed" for row in task_events)
    current_cost = sum(float(row.get("metadata", {}).get("estimated_cost_usd", 0) or 0) for row in task_events if row.get("event_type") == "api_call_completed")
    limit = analytics_config(analytics_dir)["task_cost_limit_usd"]
    near_limit_ratio = analytics_config(analytics_dir).get("near_limit_ratio", 0.8)
    projected = current_cost + cost
    if projected > limit and not confirmed:
        result = telemetry_event(
            {
                "task_id": task_id,
                "event_type": "cost_limit_exceeded",
                "stage": payload.get("stage"),
                "worker": payload.get("worker"),
                "status": "paused_cost_limit",
                "metadata": {"provider": provider, "tool": tool, "current_cost_usd": current_cost, "projected_cost_usd": projected, "limit_usd": limit},
            },
            analytics_dir,
        )
        return {"allowed": False, "paused": True, "confirmation_required": True, "alert": result["event"]}
    result = telemetry_event(
        {
            "task_id": task_id,
            "event_type": "api_call_completed",
            "stage": payload.get("stage"),
            "worker": payload.get("worker"),
            "status": payload.get("status", "completed"),
            "duration_ms": payload.get("duration_ms", 0),
            "metadata": {"provider": provider, "tool": tool, "estimated_cost_usd": cost},
        },
        analytics_dir,
    )
    near_limit = projected >= limit * near_limit_ratio
    warning = "预计本次调用后将达到预算的 {:.0f}%".format(projected / limit * 100) if near_limit else None
    return {"allowed": True, "paused": False, "near_limit": near_limit, "warning": warning, "task_cost_usd": round(projected, 4), "event": result["event"]}


def confirm_cost_limit(payload, analytics_dir=ANALYTICS_DIR):
    return telemetry_event(
        {
            "task_id": payload.get("task_id"),
            "event_type": "cost_limit_confirmed",
            "status": "running",
            "metadata": {"confirmed_by": payload.get("confirmed_by", "user")},
        },
        analytics_dir,
    )


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))]


def monitoring_summary(analytics_dir=ANALYTICS_DIR, feedback_dir=FEEDBACK_DIR):
    events = read_jsonl(analytics_dir / "events.jsonl")
    grouped = {}
    for event in events:
        grouped.setdefault(event["task_id"], []).append(event)
    tasks = []
    for task_id, task_events in grouped.items():
        task_events.sort(key=lambda row: row["timestamp"])
        types = [row["event_type"] for row in task_events]
        unresolved_cost = max([index for index, value in enumerate(types) if value == "cost_limit_exceeded"] + [-1]) > max([index for index, value in enumerate(types) if value == "cost_limit_confirmed"] + [-1])
        unresolved_failure = max([index for index, value in enumerate(types) if value == "worker_failed"] + [-1]) > max([index for index, value in enumerate(types) if value in {"worker_completed", "worker_retried"}] + [-1])
        if unresolved_cost:
            status = "paused_cost_limit"
        elif "task_exported" in types:
            status = "completed"
        elif unresolved_failure:
            status = "needs_attention"
        else:
            status = "running"
        latest_stage = next((row.get("stage") for row in reversed(task_events) if row.get("stage")), "protocol")
        api_cost = sum(float(row.get("metadata", {}).get("estimated_cost_usd", 0) or 0) for row in task_events if row["event_type"] == "api_call_completed")
        tasks.append({
            "task_id": task_id,
            "status": status,
            "stage": latest_stage,
            "started_at": task_events[0]["timestamp"],
            "updated_at": task_events[-1]["timestamp"],
            "api_calls": sum(row["event_type"] == "api_call_completed" for row in task_events),
            "estimated_cost_usd": round(api_cost, 4),
        })
    tasks.sort(key=lambda row: row["updated_at"], reverse=True)

    started = [row for row in events if row["event_type"] == "task_started"]
    started_ids = {row["task_id"] for row in started}
    completed_ids = {row["task_id"] for row in events if row["event_type"] == "task_exported"} & started_ids
    start_by_task = {row["task_id"]: row for row in started}
    task_durations = []
    for row in events:
        if row["event_type"] == "task_exported" and row["task_id"] in start_by_task:
            start = datetime.fromisoformat(start_by_task[row["task_id"]]["timestamp"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            task_durations.append((end - start).total_seconds() * 1000)

    stage_durations = {}
    for row in events:
        if row["event_type"] == "stage_completed" and row.get("stage") in {"search", "screening", "extraction", "synthesis"}:
            stage_durations.setdefault(row["stage"], []).append(row.get("duration_ms", 0))
    duration_stats = {}
    for stage in ("search", "screening", "extraction", "synthesis"):
        values = stage_durations.get(stage, [])
        duration_stats[stage] = {
            "count": len(values),
            "average_ms": round(statistics.mean(values), 1) if values else None,
            "p50_ms": percentile(values, 0.5),
            "p95_ms": percentile(values, 0.95),
        }

    interventions = [row for row in events if row["event_type"] == "manual_intervention"]
    opportunity_events = [row for row in events if row["event_type"] in {"manual_intervention", "intervention_opportunity"}]
    intervention_breakdown = {}
    for kind in ("screening", "extraction", "novelty"):
        selected = [row for row in interventions if row.get("intervention_type") == kind]
        changed = {(row["task_id"], row.get("target_id")) for row in selected}
        denominators = {}
        for row in opportunity_events:
            if row.get("intervention_type") != kind:
                continue
            denominators[(row["task_id"], kind)] = max(denominators.get((row["task_id"], kind), 0), row.get("total_units", 0))
        total_units = sum(denominators.values())
        intervention_breakdown[kind] = {"changed": len(changed), "total_units": total_units, "rate": len(changed) / total_units if total_units else None}
    changed_total = sum(value["changed"] for value in intervention_breakdown.values())
    opportunities = sum(value["total_units"] for value in intervention_breakdown.values())

    outputs = [row for row in events if row["event_type"] == "output_exported"]
    cited_items = sum(int(row.get("metadata", {}).get("cited_items", 0) or 0) for row in outputs)
    output_items = sum(int(row.get("metadata", {}).get("total_items", 0) or 0) for row in outputs)
    api_calls = [row for row in events if row["event_type"] == "api_call_completed"]
    provider_costs = {}
    for row in api_calls:
        provider = row.get("metadata", {}).get("provider", "unknown")
        bucket = provider_costs.setdefault(provider, {"calls": 0, "estimated_cost_usd": 0.0})
        bucket["calls"] += 1
        bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"] + float(row.get("metadata", {}).get("estimated_cost_usd", 0) or 0), 4)
    satisfaction = [row for row in events if row["event_type"] == "satisfaction_submitted"]
    scores = [int(row.get("metadata", {}).get("score", 0)) for row in satisfaction]
    feedback = feedback_summary(feedback_dir)
    workers = {}
    for row in events:
        if row["event_type"] not in {"worker_completed", "worker_failed", "worker_retried"}:
            continue
        worker = row.get("worker")
        if not worker:
            continue
        bucket = workers.setdefault(worker, {"completed": 0, "failed": 0, "retries": 0})
        if row["event_type"] == "worker_completed":
            bucket["completed"] += 1
        elif row["event_type"] == "worker_failed":
            bucket["failed"] += 1
        elif row["event_type"] == "worker_retried":
            bucket["retries"] += 1
    worker_rows = []
    for worker, counts in sorted(workers.items()):
        attempts = counts["completed"] + counts["failed"]
        worker_rows.append({"worker": worker, **counts, "failure_rate": counts["failed"] / attempts if attempts else None})
    alerts = []
    for row in events:
        if row["event_type"] != "cost_limit_exceeded":
            continue
        alert = dict(row)
        alert["resolved"] = any(candidate["task_id"] == row["task_id"] and candidate["event_type"] == "cost_limit_confirmed" and candidate["timestamp"] >= row["timestamp"] for candidate in events)
        alerts.append(alert)
    return {
        "generated_at": utc_now(),
        "config": analytics_config(analytics_dir),
        "metrics": {
            "task_completion_rate": len(completed_ids) / len(started_ids) if started_ids else None,
            "started_tasks": len(started_ids),
            "completed_tasks": len(completed_ids),
            "average_task_duration_ms": round(statistics.mean(task_durations), 1) if task_durations else None,
            "stage_durations": duration_stats,
            "manual_intervention_rate": changed_total / opportunities if opportunities else None,
            "interventions": intervention_breakdown,
            "extraction_accuracy": feedback["extraction"]["accuracy"],
            "source_traceability_rate": cited_items / output_items if output_items else None,
            "cited_items": cited_items,
            "output_items": output_items,
            "api_calls": len(api_calls),
            "estimated_api_cost_usd": round(sum(value["estimated_cost_usd"] for value in provider_costs.values()), 4),
            "provider_costs": provider_costs,
            "satisfaction_average": round(statistics.mean(scores), 2) if scores else None,
            "satisfaction_count": len(scores),
        },
        "tasks": tasks,
        "workers": worker_rows,
        "cost_alerts": alerts[-20:],
    }


def ab_testing_summary(analytics_dir=ANALYTICS_DIR, feedback_dir=FEEDBACK_DIR, task_store=None):
    """Compare the fixed per-task control and experiment pipelines."""
    store = task_store or TASK_STORE
    events = read_jsonl(analytics_dir / "events.jsonl")
    groups = store.experiment_groups()
    feedback = read_jsonl(feedback_dir / "extraction.jsonl")
    result = {}
    for group in ("control", "experiment"):
        task_ids = {task_id for task_id, assigned in groups.items() if assigned == group}
        task_events = [event for event in events if event.get("task_id") in task_ids]
        started = {event["task_id"] for event in task_events if event.get("event_type") == "task_started"}
        completed = {event["task_id"] for event in task_events if event.get("event_type") == "task_exported"}
        starts = {event["task_id"]: event for event in task_events if event.get("event_type") == "task_started"}
        durations = []
        for event in task_events:
            if event.get("event_type") != "task_exported" or event.get("task_id") not in starts:
                continue
            start = datetime.fromisoformat(starts[event["task_id"]]["timestamp"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
            durations.append((end - start).total_seconds() * 1000)
        opportunities = sum(int(event.get("total_units", 0) or 0) for event in task_events if event.get("event_type") == "intervention_opportunity")
        interventions = {(event.get("task_id"), event.get("target_id")) for event in task_events if event.get("event_type") == "manual_intervention"}
        extraction_events = [event for event in feedback if event.get("task_id") in task_ids]
        corrected_cells = {(event.get("task_id"), event.get("paper_id"), event.get("field")) for event in extraction_events}
        total_cells = max([int(event.get("total_cells", 0) or 0) for event in extraction_events] + [0])
        satisfaction = [event for event in task_events if event.get("event_type") == "satisfaction_submitted"]
        scores = [int(event.get("metadata", {}).get("score")) for event in satisfaction if event.get("metadata", {}).get("score") is not None]
        nps_scores = [int(event.get("metadata", {}).get("nps_score")) for event in satisfaction if event.get("metadata", {}).get("nps_score") is not None]
        promoters = sum(score >= 9 for score in nps_scores)
        detractors = sum(score <= 6 for score in nps_scores)
        result[group] = {
            "assigned_tasks": len(task_ids),
            "started_tasks": len(started),
            "completed_tasks": len(completed & started),
            "task_completion_rate": len(completed & started) / len(started) if started else None,
            "average_task_duration_ms": round(statistics.mean(durations), 1) if durations else None,
            "manual_intervention_rate": len(interventions) / opportunities if opportunities else None,
            "interventions": len(interventions),
            "extraction_accuracy": (total_cells - len(corrected_cells)) / total_cells if total_cells else None,
            "satisfaction_average": round(statistics.mean(scores), 2) if scores else None,
            "satisfaction_count": len(scores),
            "nps": round((promoters - detractors) / len(nps_scores) * 100, 1) if nps_scores else None,
            "nps_count": len(nps_scores),
        }
    return {"generated_at": utc_now(), "assignment": {"control": result["control"]["assigned_tasks"], "experiment": result["experiment"]["assigned_tasks"]}, "groups": result}


def xml_escape(value):
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def xlsx_bytes(rows):
    shared = []
    for row in rows:
        for value in row:
            value = str(value or "")
            if value not in shared:
                shared.append(value)
    shared_xml = "".join("<si><t>{}</t></si>".format(xml_escape(v)) for v in shared)
    sheet_rows = []
    for ridx, row in enumerate(rows, 1):
        cells = []
        for cidx, value in enumerate(row):
            col = ""
            n = cidx + 1
            while n:
                n, rem = divmod(n - 1, 26)
                col = chr(65 + rem) + col
            idx = shared.index(str(value or ""))
            cells.append('<c r="{}{}" t="s"><v>{}</v></c>'.format(col, ridx, idx))
        sheet_rows.append("<row r=\"{}\">{}</row>".format(ridx, "".join(cells)))
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Evidence Table" sheetId="1" r:id="rId1"/></sheets></workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>""",
        "xl/sharedStrings.xml": '<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{}" uniqueCount="{}">{}</sst>'.format(len(shared), len(shared), shared_xml),
        "xl/worksheets/sheet1.xml": '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{}</sheetData></worksheet>'.format("".join(sheet_rows)),
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def docx_bytes(text):
    paragraphs = "".join("<w:p><w:r><w:t xml:space=\"preserve\">{}</w:t></w:r></w:p>".format(xml_escape(line)) for line in text.splitlines() or [""])
    document = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{}<w:sectPr/></w:body></w:document>'.format(paragraphs)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def bundle_bytes():
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("evidence-table.csv", "演示状态,论文,研究设计,样本量,人群/地域,暴露变量,结局变量,统计方法,偏倚风险,来源\n演示样例,Compound climate extremes and alpine productivity,遥感面板,2416,青藏高原草地,热旱复合指数,NPP / 恢复力,GAM + 空间滞后,低,无真实 DOI；待 MCP 替换\n")
        archive.writestr("screening-decisions.csv", "paper_id,decision,reason,source\np1,include,Demo decision,DEMO ONLY; no MCP citation\n")
        archive.writestr("novelty-report.md", "# Novelty Analysis\n\nDemo only. No Edison Precedent task has been executed.\n\n## Sources\n\nNo real MCP citations are present.\n")
        archive.writestr("sources.json", json.dumps({"generated_at": utc_now(), "demo": True, "sources": [], "warning": "No real MCP citations are present."}, ensure_ascii=False, indent=2))
    return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    CORS_ORIGINS = {"null", "http://127.0.0.1:4174", "http://localhost:4174"}

    def send_cors_headers(self):
        origin = self.headers.get("Origin")
        if origin in self.CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def model_request_allowed(self):
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        allowed, limit = allow_model_request(forwarded or self.client_address[0])
        if not allowed:
            self._send_json({"error": "model_rate_limit", "message": "Model request limit reached.", "limit_per_hour": limit}, 429)
        return allowed

    def send_bytes(self, data, content_type, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_cors_headers()
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(filename))
        self.end_headers()
        self.wfile.write(data)

    def json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            self._send_json({"status": "ok", "mode": "public-agent", "model": model_status(), "mcp": mcp_config(), "pipeline": {"search": "live MCP required", "workers": ["SearchWorker", "ScreeningWorker", "ExtractionWorker", "NoveltyWorker", "SynthesisWorker"]}})
            return
        if path == "/api/feedback/summary":
            self.send_bytes(json.dumps(feedback_summary(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if path == "/api/admin/monitoring":
            self.send_bytes(json.dumps(monitoring_summary(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if path == "/api/admin/ab-testing":
            self.send_bytes(json.dumps(ab_testing_summary(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if path == "/api/tasks":
            self.send_bytes(json.dumps({"tasks": TASK_STORE.recent()}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "tasks":
            task_id = parts[2]
            if len(parts) == 3:
                result = TASK_STORE.get(task_id)
            elif len(parts) == 4 and parts[3] == "gates":
                result = {"task_id": task_id, "gates": TASK_STORE.gates(task_id)}
            else:
                result = None
            if result is not None:
                self.send_bytes(json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            self.send_error(404)
            return
        file_path = (ROOT / path.lstrip("/")).resolve()
        if ROOT not in file_path.parents and file_path != ROOT:
            self.send_error(403)
            return
        if file_path.is_dir():
            file_path = ROOT / "index.html"
        if not file_path.exists():
            self.send_error(404)
            return
        self.send_bytes(file_path.read_bytes(), mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_POST(self):
        try:
            payload = self.json_body()
        except (json.JSONDecodeError, ValueError):
            self._send_error("invalid_json", 400)
            return
        if self.path == "/api/protocol":
            question = str(payload.get("question", "")).strip()
            if not question:
                self._send_error("question_required", 400)
                return
            if not model_status()["configured"]:
                self._send_json({"error": "model_not_configured", "message": "A server-side model API key is required."}, 503)
                return
            if not self.model_request_allowed():
                return
            try:
                result = generate_protocol_with_model(question, payload.get("outputs") or [])
            except ModelError as exc:
                self._send_json({"error": "model_request_failed", "message": str(exc)}, 502)
                return
            result["timestamp"] = utc_now()
            task_id = payload.get("task_id")
            if task_id:
                if not TASK_STORE.get(task_id):
                    TASK_STORE.create(task_id=task_id, session_id=payload.get("session_id"), stage="protocol", status="running", input_data={"question": question, "outputs": payload.get("outputs")})
                task = TASK_STORE.get(task_id)
                state = dict(task.get("state") or {})
                state.update({"protocol": result, "next_gate": "protocol_confirmation"})
                TASK_STORE.checkpoint(task_id, stage="protocol", status="needs_attention", state=state)
            self.send_bytes(json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if self.path == "/api/agent/chat":
            task_id = str(payload.get("task_id", "")).strip()
            message = str(payload.get("message", "")).strip()
            if not task_id or not message:
                self._send_error("task_id_and_message_required", 400)
                return
            if not model_status()["configured"]:
                self._send_json({"error": "model_not_configured", "message": "A server-side model API key is required."}, 503)
                return
            if not self.model_request_allowed():
                return
            task = TASK_STORE.get(task_id)
            if not task:
                self._send_error("unknown_task", 404)
                return
            try:
                reply = agent_reply(message, task)
            except ModelError as exc:
                self._send_json({"error": "model_request_failed", "message": str(exc)}, 502)
                return
            state = dict(task.get("state") or {})
            messages = list(state.get("agent_messages") or [])
            messages.extend((
                {"role": "user", "content": message, "timestamp": utc_now()},
                {"role": "assistant", "content": reply, "timestamp": utc_now(), "evidence_type": "model_advice"},
            ))
            state["agent_messages"] = messages[-40:]
            TASK_STORE.checkpoint(task_id, state=state)
            telemetry_event({"task_id": task_id, "event_type": "agent_message_completed", "stage": task.get("stage"), "status": "completed", "model": model_status()})
            self._send_json({"reply": reply, "model": model_status(), "task_id": task_id, "evidence_type": "model_advice"})
            return
        if self.path == "/api/agent/run":
            task_id = str(payload.get("task_id", "")).strip()
            stage = str(payload.get("stage", "search")).strip()
            question = str(payload.get("question", "")).strip()
            if not task_id or not stage or not question:
                self._send_error("task_id_stage_and_question_required", 400)
                return
            task = TASK_STORE.get(task_id)
            if not task:
                self._send_error("unknown_task", 404)
                return
            if not mcp_config()["elicit"] and not mcp_config()["edison"]:
                self._send_json({"error": "mcp_not_configured", "message": "Elicit/Edison MCP credentials are not configured on the server.", "mcp": mcp_config()}, 503)
                return
            result = PIPELINE.start(task_id, stage, question, payload.get("protocol") or (task.get("state") or {}).get("protocol") or {}, payload.get("contribution"))
            self._send_json(result)
            return
        if self.path == "/api/tasks":
            try:
                result = TASK_STORE.create(
                    task_id=payload.get("task_id"),
                    session_id=payload.get("session_id"),
                    provider=payload.get("provider", ""),
                    tool=payload.get("tool", ""),
                    stage=payload.get("stage", "protocol"),
                    status=payload.get("status", "running"),
                    input_data=payload.get("input"),
                    state=payload.get("state"),
                    experiment_group=payload.get("experiment_group"),
                )
            except (TypeError, ValueError) as exc:
                self._send_error(str(exc), 400)
                return
            self._send_json(result)
            return
        parts = [part for part in self.path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "tasks":
            task_id = parts[2]
            try:
                if len(parts) == 4 and parts[3] == "checkpoint":
                    result = TASK_STORE.checkpoint(task_id, session_id=payload.get("session_id"), provider=payload.get("provider"), tool=payload.get("tool"), stage=payload.get("stage"), status=payload.get("status"), input_data=payload.get("input"), state=payload.get("state"), error=payload.get("error"))
                elif len(parts) == 4 and parts[3] == "resume":
                    result = TASK_STORE.resume(task_id)
                elif len(parts) == 4 and parts[3] == "failure":
                    result = TASK_STORE.failure(task_id, payload.get("error") or payload)
                elif len(parts) == 4 and parts[3] == "retry":
                    result = TASK_STORE.retry(task_id, payload.get("tool"))
                elif len(parts) == 4 and parts[3] == "skip":
                    result = TASK_STORE.skip(task_id, payload.get("reason", "user_requested_skip"))
                elif len(parts) == 5 and parts[3] == "gates" and parts[4] == "confirm":
                    result = TASK_STORE.confirm_gate(task_id, payload.get("gate_name"), payload.get("confirmed_by", "user"), payload.get("metadata"))
                else:
                    self.send_error(404)
                    return
            except KeyError as exc:
                self._send_error(str(exc), 404)
                return
            except (TypeError, ValueError) as exc:
                self._send_error(str(exc), 400)
                return
            self._send_json(result)
            return
        if self.path == "/api/export/xlsx":
            self.send_bytes(xlsx_bytes(payload.get("rows", [])), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "evidence-table.xlsx")
            return
        if self.path == "/api/export/docx":
            self.send_bytes(docx_bytes(payload.get("text", "")), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "research-evidence-report.docx")
            return
        if self.path == "/api/export/bundle":
            self.send_bytes(bundle_bytes(), "application/zip", "research-evidence-csv-bundle.zip")
            return
        feedback_handlers = {
            "/api/feedback/screening": screening_feedback,
            "/api/feedback/extraction": extraction_feedback,
            "/api/feedback/novelty": novelty_feedback,
        }
        if self.path in feedback_handlers:
            try:
                result = feedback_handlers[self.path](payload)
            except (TypeError, ValueError) as exc:
                data = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_bytes(json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        telemetry_handlers = {
            "/api/telemetry/event": telemetry_event,
            "/api/telemetry/api-call": api_call_telemetry,
            "/api/telemetry/satisfaction": satisfaction_feedback,
            "/api/telemetry/interview": satisfaction_feedback,
            "/api/telemetry/cost-confirmation": confirm_cost_limit,
            "/api/admin/cost-limit": save_analytics_config,
        }
        if self.path in telemetry_handlers:
            try:
                result = telemetry_handlers[self.path](payload)
            except (TypeError, ValueError) as exc:
                data = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_bytes(json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        self.send_error(404)

    def _send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_cors_headers()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, message, status=400):
        self._send_json({"error": message}, status)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 4174))
    host = os.environ.get("HOST", "127.0.0.1")
    print("Research Evidence Agent: http://{}:{}".format(host, port))
    ThreadingHTTPServer((host, port), Handler).serve_forever()
