#!/usr/bin/env python3
"""Supervisor state machine for the MCP-backed research evidence pipeline."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "0.2"
GATES = {
    "protocol": "Confirm the research question, scope, eligibility, and extraction fields.",
    "post_search": "Confirm that the merged candidate set and deduplication are acceptable.",
    "post_screening": "Confirm screening decisions, exclusion reasons, and PRISMA counts.",
    "pre_synthesis": "Confirm extraction and novelty outputs before synthesis.",
    "quality_intervention": "A stage failed its quality gate twice. Review the failure, adjust the task or criteria, then confirm to retry.",
}
DEFAULT_QUALITY_POLICY = {
    "screening_min_ratio": 0.02,
    "screening_max_ratio": 0.80,
    "screening_ratio_min_candidates": 5,
    "extraction_max_missing_rate": 0.20,
    "extraction_critical_fields": [
        "study_design",
        "sample_size",
        "effect_measures",
        "statistical_methods",
    ],
    "max_automatic_retries": 1,
}
PIPELINE = [
    {
        "stage": "search",
        "worker": "SearchWorker",
        "depends_on": [],
        "tools": [
            {"server": "elicit", "tool": "search_papers", "mode": "parallel"},
            {"server": "edison", "tool": "edison_quick_search_agent", "mode": "parallel"},
        ],
        "output": "candidates with paper_id, DOI or URL, title, abstract, and source provenance",
    },
    {
        "stage": "screening",
        "worker": "ScreeningWorker",
        "depends_on": ["search"],
        "tools": [
            {"server": "elicit", "tool": "create_systematic_review", "mode": "task"},
            {"server": "elicit", "tool": "get_systematic_review", "mode": "poll"},
        ],
        "output": "abstract and full-text decisions with exclusion reasons and PRISMA counts",
    },
    {
        "stage": "extraction",
        "worker": "ExtractionWorker",
        "depends_on": ["screening"],
        "tools": [
            {"server": "elicit", "tool": "get_systematic_review", "mode": "read_extraction_output"},
            {"server": "edison", "tool": "edison_deep_search_agent", "mode": "task"},
        ],
        "output": "study design, sample size, effect measures, statistics, methods, and source spans",
    },
    {
        "stage": "novelty",
        "worker": "NoveltyWorker",
        "depends_on": ["extraction"],
        "tools": [
            {"server": "edison", "tool": "edison_precedent_search_agent", "mode": "task"},
        ],
        "output": "closest work, partial precedent, method analogue, and bounded novelty verdict",
    },
    {
        "stage": "synthesis",
        "worker": "SynthesisWorker",
        "depends_on": ["search", "screening", "extraction", "novelty"],
        "tools": [
            {"server": "edison", "tool": "edison_deep_search_agent", "mode": "task"},
        ],
        "output": "evidence table, method comparison matrix, narrative synthesis, and citations",
    },
]


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def require_project(project):
    state_path = project / "workflow/state.json"
    if not state_path.exists():
        raise ValueError("No Supervisor workflow found at {}".format(state_path))
    return state_path


def pipeline_item(stage):
    for item in PIPELINE:
        if item["stage"] == stage:
            return item
    return None


def stage_map(state):
    return {item["stage"]: item for item in state["stages"]}


def record_event(project, action, details):
    append_jsonl(
        project / "audit/events.jsonl",
        {"timestamp": now(), "action": action, "details": details},
    )


def set_gate(state, gate):
    state["status"] = "awaiting_confirmation"
    state["gate"] = gate
    state["gate_message"] = GATES[gate]


def workflow_init(args):
    project = Path(args.project).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    state_path = project / "workflow/state.json"
    if state_path.exists() and not args.force:
        raise ValueError("Workflow already exists; use --force to replace it")
    stages = []
    for item in PIPELINE:
        stages.append(
            {
                "stage": item["stage"],
                "worker": item["worker"],
                "status": "pending",
                "artifact": None,
                "artifact_sha256": None,
                "source_refs": [],
                "mcp_calls": [],
                "started_at": None,
                "completed_at": None,
                "attempts": 0,
                "quality_gate": None,
            }
        )
    state = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now(),
        "updated_at": now(),
        "status": "awaiting_confirmation",
        "gate": "protocol",
        "gate_message": GATES["protocol"],
        "current_stage": "search",
        "question": args.question,
        "protocol_path": str((project / "protocol.json").resolve()) if (project / "protocol.json").exists() else None,
        "stages": stages,
        "global_state": {
            "candidate_paper_ids": [],
            "screening_decisions": [],
            "extraction_paper_ids": [],
            "novelty_claim_ids": [],
            "synthesis_artifact": None,
        },
        "shared_state": {},
        "quality_policy": DEFAULT_QUALITY_POLICY,
        "quality_gates": [],
        "confirmations": [],
    }
    write_json(state_path, state)
    record_event(project, "supervisor_initialized", {"question": args.question, "schema_version": SCHEMA_VERSION})
    print(json.dumps(state, ensure_ascii=False, indent=2))


def workflow_status(args):
    project = Path(args.project).expanduser().resolve()
    state = read_json(require_project(project))
    print(json.dumps(state, ensure_ascii=False, indent=2))


def generate_protocol(question, outputs=None):
    """Generate a conservative, editable protocol draft without calling an MCP tool."""
    question = " ".join((question or "").split())
    if not question:
        raise ValueError("Question must not be empty")
    outputs = outputs or ["evidence", "narrative", "matrix", "novelty"]
    lower = question.lower()
    framework = "PICO" if any(token in lower for token in ("intervention", "treatment", "trial", "干预", "治疗")) else "PEO"
    population = "研究问题中指定的人群、生态系统或研究对象"
    exposure = "研究问题中指定的暴露、现象或解释变量"
    outcome = "研究问题中指定的结果变量、效应或结局"
    if any(token in lower for token in ("青藏高原", "tibetan plateau")):
        population = "青藏高原生态系统，重点关注草地、湿地和高寒植被"
    if any(token in lower for token in ("高温", "干旱", "heat", "drought")):
        exposure = "复合高温与干旱事件，包含热浪、降水异常和 SPEI 等指标"
    if any(token in lower for token in ("生产力", "ndvi", "npp", "productivity")):
        outcome = "生态系统生产力、NDVI/NPP、物候变化和恢复力"
    query = '"{}" AND ("{}" OR "{}")'.format(question[:120], population[:80], outcome[:80])
    return {
        "framework": framework,
        "population": population,
        "exposure": exposure,
        "outcome": outcome,
        "query": query,
        "outputs": outputs,
        "generated_by": "supervisor_local",
        "editable": True,
        "source_citations": [],
        "limitations": ["这是可编辑的协议草案，不是文献检索结果。", "真实论文来源将在 SearchWorker 调用 MCP 后写入。"],
        "timestamp": now(),
    }


def workflow_protocol(args):
    print(json.dumps(generate_protocol(args.question, args.output), ensure_ascii=False, indent=2))


def workflow_next(args):
    project = Path(args.project).expanduser().resolve()
    state_path = require_project(project)
    state = read_json(state_path)
    if state["status"] == "awaiting_confirmation":
        print(json.dumps({"kind": "confirmation", "gate": state["gate"], "message": state["gate_message"]}, ensure_ascii=False, indent=2))
        return
    if state["status"] == "completed":
        print(json.dumps({"kind": "complete", "message": "Workflow is complete."}, ensure_ascii=False, indent=2))
        return
    item = pipeline_item(state["current_stage"])
    if not item:
        raise ValueError("Unknown current stage: {}".format(state["current_stage"]))
    stage = stage_map(state)[item["stage"]]
    if stage["status"] in {"pending", "failed_quality_gate"}:
        stage["status"] = "running"
        stage["started_at"] = now()
        stage["attempts"] += 1
        state["status"] = "running"
        state["updated_at"] = now()
        write_json(state_path, state)
        record_event(project, "worker_started", {"stage": item["stage"], "worker": item["worker"]})
    payload = {
        "kind": "worker",
        "stage": item["stage"],
        "worker": item["worker"],
        "question": state["question"],
        "tools": item["tools"],
        "depends_on": item["depends_on"],
        "input_artifacts": {
            dependency: stage_map(state)[dependency]["artifact"]
            for dependency in item["depends_on"]
        },
        "global_state": state["global_state"],
        "expected_output": item["output"],
        "artifact_contract": "references/schemas.md#worker-result-envelope",
        "do_not": ["do not advance the workflow", "do not omit DOI/URL provenance", "do not claim unbounded novelty"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_source_ref(ref):
    return isinstance(ref, dict) and bool(ref.get("doi") or ref.get("url"))


def validate_worker_artifact(path, expected_worker, expected_stage):
    try:
        artifact = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Cannot read Worker artifact: {}".format(exc))
    if artifact.get("worker") != expected_worker:
        raise ValueError("Artifact worker {} does not match {}".format(artifact.get("worker"), expected_worker))
    if artifact.get("status") != "completed":
        raise ValueError("Worker artifact status must be completed")
    if not artifact.get("task_id"):
        raise ValueError("Worker artifact must contain task_id")
    source_refs = artifact.get("source_citations") or artifact.get("source_refs") or []
    if not source_refs or not all(valid_source_ref(ref) for ref in source_refs):
        raise ValueError("Worker artifact must contain source_citations with DOI or URL")
    mcp_calls = artifact.get("mcp_calls") or []
    if not mcp_calls:
        raise ValueError("Worker artifact must contain mcp_calls")
    result = artifact.get("result")
    if not isinstance(result, dict):
        raise ValueError("Worker artifact result must be an object")
    required = {
        "search": ("candidates",),
        "screening": ("decisions", "prisma"),
        "extraction": ("extractions",),
        "novelty": ("claims",),
        "synthesis": ("evidence_table", "method_matrix", "narrative"),
    }[expected_stage]
    missing = [key for key in required if key not in result]
    if missing:
        raise ValueError("{} artifact missing result fields: {}".format(expected_worker, ", ".join(missing)))
    return artifact


def is_missing(value):
    if value is None or value == "" or value == [] or value == {}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"unknown", "not reported", "not available", "n/a"}:
        return True
    return False


def evaluate_quality_gate(state, stage, result):
    policy = state["quality_policy"]
    details = {}
    if stage == "search":
        candidate_count = len(result.get("candidates") or [])
        details = {"candidate_count": candidate_count, "required": "> 0"}
        passed = candidate_count > 0
        reason = "candidate paper count is zero" if not passed else "candidate paper count is positive"
    elif stage == "screening":
        candidate_count = len(state["global_state"].get("candidate_paper_ids") or [])
        decisions = result.get("decisions") or []
        full_text = [row for row in decisions if row.get("stage") == "full_text"]
        counted = full_text or decisions
        included_count = sum(1 for row in counted if row.get("decision") == "include")
        ratio = included_count / candidate_count if candidate_count else 0.0
        if candidate_count < policy["screening_ratio_min_candidates"]:
            passed = 0 < included_count <= candidate_count
            expected = "1..candidate_count for small candidate sets"
        else:
            passed = (
                included_count > 0
                and included_count <= candidate_count
                and policy["screening_min_ratio"] <= ratio <= policy["screening_max_ratio"]
            )
            expected = "{:.0%}..{:.0%}".format(policy["screening_min_ratio"], policy["screening_max_ratio"])
        details = {
            "candidate_count": candidate_count,
            "included_count": included_count,
            "inclusion_ratio": ratio,
            "expected": expected,
        }
        reason = "screening inclusion ratio is within policy" if passed else "screening inclusion ratio is outside policy"
    elif stage == "extraction":
        rows = result.get("extractions") or []
        critical_fields = policy["extraction_critical_fields"]
        total = len(rows) * len(critical_fields)
        missing = sum(1 for row in rows for field in critical_fields if is_missing(row.get(field)))
        rate = missing / total if total else 1.0
        passed = bool(rows) and rate < policy["extraction_max_missing_rate"]
        details = {
            "extraction_count": len(rows),
            "critical_fields": critical_fields,
            "missing_cells": missing,
            "total_cells": total,
            "missing_rate": rate,
            "required": "< {:.0%}".format(policy["extraction_max_missing_rate"]),
        }
        reason = "critical-field missing rate is below threshold" if passed else "critical-field missing rate is too high"
    else:
        passed = True
        reason = "no deterministic quality threshold configured for this stage"
        details = {"check": "source citations and schema validation"}
    return {
        "stage": stage,
        "passed": passed,
        "reason": reason,
        "details": details,
        "timestamp": now(),
    }


def build_shared_entry(state, item, artifact):
    inputs = {
        dependency: {
            "task_id": state["shared_state"][dependency]["task_id"],
            "artifact": stage_map(state)[dependency]["artifact"],
            "artifact_sha256": stage_map(state)[dependency]["artifact_sha256"],
        }
        for dependency in item["depends_on"]
    }
    return {
        "task_id": artifact["task_id"],
        "stage": item["stage"],
        "input": inputs,
        "output": artifact["result"],
        "source_citations": artifact.get("source_citations") or artifact.get("source_refs") or [],
        "timestamp": now(),
    }


def update_global_state(state, stage, result):
    global_state = state["global_state"]
    if stage == "search":
        global_state["candidate_paper_ids"] = [row.get("paper_id") for row in result.get("candidates", []) if row.get("paper_id")]
    elif stage == "screening":
        global_state["screening_decisions"] = result.get("decisions", [])
    elif stage == "extraction":
        global_state["extraction_paper_ids"] = [row.get("paper_id") for row in result.get("extractions", []) if row.get("paper_id")]
    elif stage == "novelty":
        global_state["novelty_claim_ids"] = [row.get("claim_id") for row in result.get("claims", []) if row.get("claim_id")]
    elif stage == "synthesis":
        global_state["synthesis_artifact"] = True


def workflow_complete(args):
    project = Path(args.project).expanduser().resolve()
    state_path = require_project(project)
    state = read_json(state_path)
    if state["status"] != "running":
        raise ValueError("Workflow is not running; next action is {}".format(state.get("gate") or state.get("status")))
    current = stage_map(state)[state["current_stage"]]
    item = pipeline_item(current["stage"])
    artifact_path = Path(args.artifact).expanduser().resolve()
    artifact = validate_worker_artifact(artifact_path, item["worker"], item["stage"])
    shared_entry = build_shared_entry(state, item, artifact)
    state["shared_state"][item["stage"]] = shared_entry
    quality_gate = evaluate_quality_gate(state, item["stage"], artifact["result"])
    current["quality_gate"] = quality_gate
    state["quality_gates"].append(quality_gate)
    if not quality_gate["passed"]:
        current["status"] = "failed_quality_gate"
        current["artifact"] = str(artifact_path)
        current["artifact_sha256"] = sha256_file(artifact_path)
        current["source_refs"] = shared_entry["source_citations"]
        current["mcp_calls"] = artifact["mcp_calls"]
        state["updated_at"] = now()
        if current["attempts"] <= state["quality_policy"]["max_automatic_retries"]:
            state["status"] = "needs_retry"
            state["gate"] = None
            state["gate_message"] = quality_gate["reason"]
        else:
            set_gate(state, "quality_intervention")
            state["gate_message"] = "{} {}".format(GATES["quality_intervention"], quality_gate["reason"])
        append_jsonl(
            project / "workflow/runs.jsonl",
            {
                "timestamp": now(),
                "task_id": artifact["task_id"],
                "stage": item["stage"],
                "worker": item["worker"],
                "status": "failed_quality_gate",
                "quality_gate": quality_gate,
                "artifact": str(artifact_path),
            },
        )
        record_event(project, "quality_gate_failed", quality_gate)
        write_json(state_path, state)
        print(json.dumps({"completed": False, "stage": item["stage"], "status": state["status"], "quality_gate": quality_gate}, ensure_ascii=False, indent=2))
        return
    current.update(
        {
            "status": "completed",
            "artifact": str(artifact_path),
            "artifact_sha256": sha256_file(artifact_path),
            "source_refs": shared_entry["source_citations"],
            "mcp_calls": artifact["mcp_calls"],
            "completed_at": now(),
        }
    )
    update_global_state(state, item["stage"], artifact["result"])
    state["updated_at"] = now()
    if item["stage"] == "search":
        state["current_stage"] = "screening"
        set_gate(state, "post_search")
    elif item["stage"] == "screening":
        state["current_stage"] = "extraction"
        set_gate(state, "post_screening")
    elif item["stage"] == "extraction":
        state["current_stage"] = "novelty"
        state["status"] = "running"
        state["gate"] = None
        state["gate_message"] = None
    elif item["stage"] == "novelty":
        state["current_stage"] = "synthesis"
        set_gate(state, "pre_synthesis")
    elif item["stage"] == "synthesis":
        state["current_stage"] = None
        state["status"] = "completed"
        state["gate"] = None
        state["gate_message"] = None
    append_jsonl(
        project / "workflow/runs.jsonl",
        {
            "timestamp": now(),
            "task_id": artifact["task_id"],
            "stage": item["stage"],
            "worker": item["worker"],
            "status": "completed",
            "artifact": str(artifact_path),
            "artifact_sha256": current["artifact_sha256"],
        },
    )
    record_event(project, "worker_completed", {"stage": item["stage"], "worker": item["worker"], "artifact": str(artifact_path)})
    write_json(state_path, state)
    print(json.dumps({"completed": item["stage"], "next_stage": state["current_stage"], "status": state["status"], "gate": state["gate"]}, ensure_ascii=False, indent=2))


def workflow_confirm(args):
    project = Path(args.project).expanduser().resolve()
    state_path = require_project(project)
    state = read_json(state_path)
    if state["status"] != "awaiting_confirmation":
        raise ValueError("No confirmation is pending")
    if args.gate != state["gate"]:
        raise ValueError("Expected gate {}, received {}".format(state["gate"], args.gate))
    state["confirmations"].append({"gate": args.gate, "confirmed_by": args.by, "timestamp": now(), "note": args.note})
    state["status"] = "needs_retry" if args.gate == "quality_intervention" else "running"
    state["gate"] = None
    state["gate_message"] = None
    state["updated_at"] = now()
    record_event(project, "gate_confirmed", {"gate": args.gate, "confirmed_by": args.by, "note": args.note})
    write_json(state_path, state)
    print(json.dumps({"confirmed": args.gate, "next_stage": state["current_stage"], "status": state["status"]}, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--project", required=True)
    init.add_argument("--question", required=True)
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=workflow_init)
    for command, handler in (("status", workflow_status), ("next", workflow_next)):
        item = sub.add_parser(command)
        item.add_argument("--project", required=True)
        item.set_defaults(handler=handler)
    protocol = sub.add_parser("protocol")
    protocol.add_argument("--question", required=True)
    protocol.add_argument("--output", action="append", default=[])
    protocol.set_defaults(handler=workflow_protocol)
    complete = sub.add_parser("complete")
    complete.add_argument("--project", required=True)
    complete.add_argument("--artifact", required=True)
    complete.set_defaults(handler=workflow_complete)
    confirm = sub.add_parser("confirm")
    confirm.add_argument("--project", required=True)
    confirm.add_argument("--gate", required=True, choices=tuple(GATES))
    confirm.add_argument("--by", required=True)
    confirm.add_argument("--note", default="")
    confirm.set_defaults(handler=workflow_confirm)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args) or 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
