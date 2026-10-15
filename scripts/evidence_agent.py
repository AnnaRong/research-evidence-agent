#!/usr/bin/env python3
"""Deterministic project utilities for the Research Evidence Agent."""

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path


SCHEMA_VERSION = "0.1"
ALLOWED_DECISIONS = {"include", "exclude", "uncertain"}
ALLOWED_STAGES = {"title_abstract", "full_text"}
ALLOWED_NOVELTY = {
    "precedent_found",
    "partial_precedent",
    "method_analogue",
    "no_precedent_found_within_search",
    "unresolved",
}
PROJECT_FILES = (
    "search/queries.jsonl",
    "mcp/calls.jsonl",
    "corpus/records.jsonl",
    "corpus/deduped.jsonl",
    "screening/decisions.jsonl",
    "evidence/evidence.jsonl",
    "novelty/claims.jsonl",
    "audit/events.jsonl",
)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError("{}:{} invalid JSON: {}".format(path, line_number, exc))
    return rows


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_jsonl(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def audit(project, action, details):
    append_jsonl(
        project / "audit/events.jsonl",
        {"timestamp": utc_now(), "action": action, "details": details},
    )


def require_project(project):
    if not (project / "protocol.json").exists():
        raise ValueError("Not an initialized project: {}".format(project))


def normalize_doi(value):
    if not value:
        return ""
    value = str(value).strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value.rstrip(" .")


def normalize_title(value):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value)
    return " ".join(value.split())


def first_author_surname(record):
    authors = record.get("authors") or []
    if not authors:
        return ""
    name = authors[0] if isinstance(authors[0], str) else authors[0].get("name", "")
    parts = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", name.lower())
    return parts[-1] if parts else ""


def cmd_init(args):
    project = Path(args.project).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    protocol_path = project / "protocol.json"
    if protocol_path.exists() and not args.force:
        raise ValueError("protocol.json already exists; use --force to replace it")
    protocol = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "status": "draft",
        "review_type": "scoping_review",
        "question": args.question,
        "question_framework": {"type": "PCC", "elements": {}},
        "eligibility": {
            "include": [],
            "exclude": [],
            "languages": [],
            "publication_types": [],
            "date_range": {"from": None, "to": None},
        },
        "planned_sources": ["Elicit MCP", "Edison MCP"],
        "supplementary_search": ["DOI/bibliographic verification when required"],
        "extraction_fields": [
            "research_object",
            "geography",
            "observation_period",
            "spatial_scale",
            "datasets",
            "variables",
            "methods",
            "validation",
            "findings",
            "limitations",
        ],
        "human_reviewer": None,
        "human_confirmed": False,
    }
    write_json(protocol_path, protocol)
    for relative in PROJECT_FILES:
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    (project / "reports").mkdir(exist_ok=True)
    audit(project, "project_initialized", {"question": args.question, "schema_version": SCHEMA_VERSION})
    print(project)


def cmd_mcp_log(args):
    project = Path(args.project).expanduser().resolve()
    require_project(project)
    if not args.tool or not args.server:
        raise ValueError("--server and --tool are required")
    row = {
        "timestamp": utc_now(),
        "server": args.server,
        "tool": args.tool,
        "status": args.status,
        "task_id": args.task_id,
        "session_id": args.session_id,
        "input_summary": args.input_summary,
        "output_artifact": args.output_artifact,
        "warning": args.warning,
    }
    append_jsonl(project / "mcp/calls.jsonl", row)
    audit(project, "mcp_call_logged", {key: value for key, value in row.items() if value is not None})
    print(json.dumps(row, ensure_ascii=False, indent=2))


def is_preprint(record):
    record_type = (record.get("type") or "").lower()
    venue = (record.get("venue") or "").lower()
    return record_type == "preprint" or any(name in venue for name in ("arxiv", "biorxiv", "medrxiv", "ssrn"))


def record_rank(record):
    return (
        0 if is_preprint(record) else 1,
        1 if normalize_doi(record.get("doi")) else 0,
        int(record.get("cited_by_count") or 0),
    )


def records_match(left, right, threshold):
    left_doi = normalize_doi(left.get("doi"))
    right_doi = normalize_doi(right.get("doi"))
    if left_doi and right_doi and left_doi == right_doi:
        return True, "doi"
    left_title = normalize_title(left.get("title"))
    right_title = normalize_title(right.get("title"))
    if not left_title or not right_title:
        return False, ""
    similarity = SequenceMatcher(None, left_title, right_title).ratio()
    if similarity < threshold:
        return False, ""
    left_year = left.get("publication_year")
    right_year = right.get("publication_year")
    if left_year and right_year and abs(int(left_year) - int(right_year)) > 1:
        return False, ""
    left_author = first_author_surname(left)
    right_author = first_author_surname(right)
    if left_author and right_author and left_author != right_author:
        return False, ""
    return True, "fuzzy_title"


def cmd_dedupe(args):
    project = Path(args.project).expanduser().resolve()
    require_project(project)
    records = read_jsonl(project / "corpus/records.jsonl")
    unique = []
    groups = []
    # ponytail: O(n^2) is sufficient for review-scale corpora; index titles above tens of thousands.
    for record in records:
        duplicate_index = None
        match_reason = ""
        for index, existing in enumerate(unique):
            matched, reason = records_match(record, existing, args.threshold)
            if matched:
                duplicate_index = index
                match_reason = reason
                break
        if duplicate_index is None:
            row = dict(record)
            row.setdefault("duplicate_of", [])
            unique.append(row)
            continue
        existing = unique[duplicate_index]
        if record_rank(record) > record_rank(existing):
            replacement = dict(record)
            replacement["duplicate_of"] = list(existing.get("duplicate_of") or []) + [existing.get("record_id")]
            unique[duplicate_index] = replacement
            canonical = replacement.get("record_id")
            duplicate = existing.get("record_id")
        else:
            existing.setdefault("duplicate_of", []).append(record.get("record_id"))
            canonical = existing.get("record_id")
            duplicate = record.get("record_id")
        groups.append({"canonical": canonical, "duplicate": duplicate, "reason": match_reason})
    write_jsonl(project / "corpus/deduped.jsonl", unique)
    report = {
        "timestamp": utc_now(),
        "identified": len(records),
        "duplicates_removed": len(records) - len(unique),
        "after_deduplication": len(unique),
        "threshold": args.threshold,
        "groups": groups,
    }
    write_json(project / "reports/deduplication.json", report)
    audit(project, "records_deduplicated", {key: report[key] for key in ("identified", "duplicates_removed", "after_deduplication", "threshold")})
    print(json.dumps({key: report[key] for key in ("identified", "duplicates_removed", "after_deduplication")}, indent=2))


def paper_ids(project):
    return {row.get("record_id") for row in read_jsonl(project / "corpus/deduped.jsonl") if row.get("record_id")}


def validate_project(project):
    errors = []
    warnings = []
    protocol_path = project / "protocol.json"
    if not protocol_path.exists():
        return ["Missing protocol.json"], warnings
    try:
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return ["Invalid protocol.json: {}".format(exc)], warnings
    if not protocol.get("question"):
        errors.append("protocol.json: question is required")
    if not protocol.get("human_confirmed"):
        warnings.append("Protocol has not been human-confirmed")
    known_ids = paper_ids(project)
    try:
        decisions = read_jsonl(project / "screening/decisions.jsonl")
        evidence = read_jsonl(project / "evidence/evidence.jsonl")
        claims = read_jsonl(project / "novelty/claims.jsonl")
    except ValueError as exc:
        return [str(exc)], warnings
    seen_decisions = set()
    for index, row in enumerate(decisions, 1):
        label = "screening/decisions.jsonl row {}".format(index)
        if row.get("paper_id") not in known_ids:
            errors.append("{}: unknown paper_id".format(label))
        if row.get("stage") not in ALLOWED_STAGES:
            errors.append("{}: invalid stage".format(label))
        if row.get("decision") not in ALLOWED_DECISIONS:
            errors.append("{}: invalid decision".format(label))
        if not row.get("reason"):
            errors.append("{}: reason is required".format(label))
        decision_key = (row.get("paper_id"), row.get("stage"), row.get("reviewer"))
        if decision_key in seen_decisions:
            warnings.append("{}: duplicate reviewer decision".format(label))
        seen_decisions.add(decision_key)
    for index, row in enumerate(evidence, 1):
        label = "evidence/evidence.jsonl row {}".format(index)
        if row.get("paper_id") not in known_ids:
            errors.append("{}: unknown paper_id".format(label))
        for field in ("variables", "methods", "findings", "source_spans"):
            if not row.get(field):
                errors.append("{}: {} is required".format(label, field))
        for span in row.get("source_spans") or []:
            if not span.get("location") or not span.get("access_level") or not span.get("quote_or_paraphrase"):
                errors.append("{}: each source span needs location, access_level, and quote_or_paraphrase".format(label))
        if not row.get("human_verified"):
            warnings.append("{}: evidence is not human-verified".format(label))
    for index, row in enumerate(claims, 1):
        label = "novelty/claims.jsonl row {}".format(index)
        if not row.get("claim_id") or not row.get("proposed_contribution"):
            errors.append("{}: claim_id and proposed_contribution are required".format(label))
        if row.get("verdict") not in ALLOWED_NOVELTY:
            errors.append("{}: invalid verdict".format(label))
        referenced = set(row.get("supporting_paper_ids") or []) | set(row.get("contradicting_paper_ids") or [])
        unknown = sorted(referenced - known_ids)
        if unknown:
            errors.append("{}: unknown paper ids {}".format(label, ", ".join(unknown)))
        if row.get("verdict") == "no_precedent_found_within_search":
            if not row.get("search_queries") or not row.get("scope_limit"):
                errors.append("{}: bounded no-precedent verdict needs search_queries and scope_limit".format(label))
        if not row.get("human_confirmed"):
            warnings.append("{}: novelty verdict is not human-confirmed".format(label))
    return errors, warnings


def status_counts(project):
    records = read_jsonl(project / "corpus/records.jsonl")
    deduped = read_jsonl(project / "corpus/deduped.jsonl")
    decisions = read_jsonl(project / "screening/decisions.jsonl")
    evidence = read_jsonl(project / "evidence/evidence.jsonl")
    claims = read_jsonl(project / "novelty/claims.jsonl")
    counts = {
        "identified": len(records),
        "duplicates_removed": max(0, len(records) - len(deduped)),
        "after_deduplication": len(deduped),
        "title_abstract_include": 0,
        "title_abstract_exclude": 0,
        "title_abstract_uncertain": 0,
        "full_text_include": 0,
        "full_text_exclude": 0,
        "full_text_uncertain": 0,
        "evidence_rows": len(evidence),
        "novelty_claims": len(claims),
    }
    for row in decisions:
        key = "{}_{}".format(row.get("stage"), row.get("decision"))
        if key in counts:
            counts[key] += 1
    return counts


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def export_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def cmd_report(args):
    project = Path(args.project).expanduser().resolve()
    require_project(project)
    reports = project / "reports"
    reports.mkdir(exist_ok=True)
    decisions = read_jsonl(project / "screening/decisions.jsonl")
    evidence = read_jsonl(project / "evidence/evidence.jsonl")
    claims = read_jsonl(project / "novelty/claims.jsonl")
    records = {row.get("record_id"): row for row in read_jsonl(project / "corpus/deduped.jsonl")}
    export_csv(
        reports / "screening-decisions.csv",
        decisions,
        ["paper_id", "stage", "decision", "reason", "reviewer", "human_confirmed", "timestamp"],
    )
    export_csv(
        reports / "evidence-table.csv",
        evidence,
        [
            "paper_id",
            "research_object",
            "geography",
            "observation_period",
            "spatial_scale",
            "datasets",
            "variables",
            "methods",
            "validation",
            "findings",
            "limitations",
            "source_spans",
            "extractor",
            "human_verified",
        ],
    )
    lines = [
        "# Novelty Analysis",
        "",
        "> A `no_precedent_found_within_search` result is bounded by the documented search and is not proof of global novelty.",
        "",
    ]
    if not claims:
        lines.append("No novelty claims have been recorded.")
    for claim in claims:
        lines.extend(
            [
                "## {}: {}".format(claim.get("claim_id", "Unnumbered"), claim.get("proposed_contribution", "")),
                "",
                "- Verdict: `{}`".format(claim.get("verdict", "unresolved")),
                "- Human confirmed: `{}`".format(bool(claim.get("human_confirmed"))),
                "- Scope: {}".format(claim.get("scope_limit") or "Not documented"),
                "- Rationale: {}".format(claim.get("rationale") or "Not documented"),
                "- Queries: {}".format("; ".join(claim.get("search_queries") or []) or "Not documented"),
                "",
                "### Closest and contrary evidence",
                "",
            ]
        )
        cited_ids = list(claim.get("supporting_paper_ids") or []) + list(claim.get("contradicting_paper_ids") or [])
        if not cited_ids:
            lines.append("- No linked studies.")
        for paper_id in dict.fromkeys(cited_ids):
            record = records.get(paper_id) or {}
            lines.append("- `{}`: {} ({}) DOI `{}`".format(paper_id, record.get("title", "Unknown title"), record.get("publication_year", "n.d."), record.get("doi") or "missing"))
        lines.append("")
    (reports / "novelty-analysis.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    counts = status_counts(project)
    errors, warnings = validate_project(project)
    status = {"generated_at": utc_now(), "counts": counts, "validation": {"errors": errors, "warnings": warnings}}
    write_json(reports / "status.json", status)
    status_lines = ["# Review Status", "", "## Counts", ""]
    status_lines.extend("- {}: {}".format(key, value) for key, value in counts.items())
    status_lines.extend(["", "## Validation", ""])
    status_lines.append("- Errors: {}".format(len(errors)))
    status_lines.append("- Warnings: {}".format(len(warnings)))
    for item in errors:
        status_lines.append("- ERROR: {}".format(item))
    for item in warnings:
        status_lines.append("- WARNING: {}".format(item))
    (reports / "status.md").write_text("\n".join(status_lines) + "\n", encoding="utf-8")
    audit(project, "reports_generated", {"errors": len(errors), "warnings": len(warnings), "counts": counts})
    print(reports)


def cmd_validate(args):
    project = Path(args.project).expanduser().resolve()
    require_project(project)
    errors, warnings = validate_project(project)
    for warning in warnings:
        print("WARNING: {}".format(warning))
    for error in errors:
        print("ERROR: {}".format(error), file=sys.stderr)
    print("Validation: {} error(s), {} warning(s)".format(len(errors), len(warnings)))
    return 1 if errors else 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create an auditable review project")
    init_parser.add_argument("--project", required=True)
    init_parser.add_argument("--question", required=True)
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(handler=cmd_init)

    mcp_parser = subparsers.add_parser("mcp-log", help="Record a connected Elicit or Edison MCP call")
    mcp_parser.add_argument("--project", required=True)
    mcp_parser.add_argument("--server", required=True, choices=("elicit", "edison"))
    mcp_parser.add_argument("--tool", required=True)
    mcp_parser.add_argument("--status", default="completed", choices=("started", "completed", "paused", "failed"))
    mcp_parser.add_argument("--task-id")
    mcp_parser.add_argument("--session-id")
    mcp_parser.add_argument("--input-summary")
    mcp_parser.add_argument("--output-artifact")
    mcp_parser.add_argument("--warning")
    mcp_parser.set_defaults(handler=cmd_mcp_log)

    dedupe_parser = subparsers.add_parser("dedupe", help="Deduplicate corpus records")
    dedupe_parser.add_argument("--project", required=True)
    dedupe_parser.add_argument("--threshold", type=float, default=0.92)
    dedupe_parser.set_defaults(handler=cmd_dedupe)

    report_parser = subparsers.add_parser("report", help="Export evidence, screening, novelty, and status reports")
    report_parser.add_argument("--project", required=True)
    report_parser.set_defaults(handler=cmd_report)

    validate_parser = subparsers.add_parser("validate", help="Validate audit artifacts and evidence links")
    validate_parser.add_argument("--project", required=True)
    validate_parser.set_defaults(handler=cmd_validate)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return result or 0


if __name__ == "__main__":
    sys.exit(main())
