#!/usr/bin/env python3
"""Minimal server-side LLM adapter for protocol generation and agent chat."""

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelError(RuntimeError):
    pass


def model_config():
    # Match TalentPilot's approved OpenAI-compatible gateway contract first.
    # The key stays server-side; the browser never receives it.
    gateway_key = os.environ.get("LLM_API_KEY", "").strip()
    gateway_enabled = os.environ.get("LLM_EGRESS_ENABLED", "0") == "1"
    if gateway_key and gateway_enabled:
        base_url = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
        allowed_host = os.environ.get("LLM_ALLOWED_HOST", "").strip()
        if base_url.startswith("https://") and (not allowed_host or base_url.split("/", 3)[2] == allowed_host):
            return {
                "provider": "openai-compatible",
                "base_url": base_url,
                "api_key": gateway_key,
                "model": os.environ.get("LLM_MODEL", "gpt-5-mini"),
                "endpoint": "chat/completions",
            }
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return {
            "provider": "anthropic",
            "base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/"),
            "api_key": os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            "endpoint": "messages",
        }
    if os.environ.get("OPENAI_API_KEY"):
        return {
            "provider": "openai",
            "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            "api_key": os.environ["OPENAI_API_KEY"],
            "model": os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
            "endpoint": "responses",
        }
    return None


def model_status():
    config = model_config()
    return {
        "configured": bool(config),
        "provider": config["provider"] if config else None,
        "model": config["model"] if config else None,
    }


def _request(url, headers, payload):
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise ModelError("Model API returned {}: {}".format(exc.code, detail)) from exc
    except (URLError, TimeoutError) as exc:
        raise ModelError("Model API request failed: {}".format(exc)) from exc


def complete(system, user, max_tokens=1400):
    config = model_config()
    if not config:
        raise ModelError("No model API key configured")
    if config["provider"] == "openai-compatible":
        payload = {"model": config["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_completion_tokens": max_tokens}
        if "json" in system.lower() or "json" in user.lower():
            payload["response_format"] = {"type": "json_object"}
        data = _request(
            config["base_url"] + "/chat/completions",
            {"Content-Type": "application/json", "Authorization": "Bearer " + config["api_key"]},
            payload,
        )
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ModelError("OpenAI-compatible gateway returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
        return str(content).strip()
    if config["provider"] == "anthropic":
        data = _request(
            config["base_url"] + "/v1/messages",
            {
                "Content-Type": "application/json",
                "x-api-key": config["api_key"],
                "Authorization": "Bearer " + config["api_key"],
                "anthropic-version": "2023-06-01",
            },
            {"model": config["model"], "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]},
        )
        return "".join(item.get("text", "") for item in data.get("content", []) if item.get("type") == "text").strip()
    data = _request(
        config["base_url"] + "/responses",
        {"Content-Type": "application/json", "Authorization": "Bearer " + config["api_key"]},
        {"model": config["model"], "instructions": system, "input": user, "max_output_tokens": max_tokens},
    )
    if data.get("output_text"):
        return data["output_text"].strip()
    return "".join(part.get("text", "") for item in data.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text").strip()


def complete_json(system, user, max_tokens=1400):
    text = complete(system + "\nReturn one valid JSON object only. Do not use Markdown fences.", user, max_tokens)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ModelError("Model did not return a JSON object")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ModelError("Model returned invalid JSON") from exc


def generate_protocol_with_model(question, outputs):
    result = complete_json(
        "You are the Supervisor of an auditable scientific evidence agent. Design only a concise search protocol. Never invent papers, citations, search results, or effect sizes. Keep population, exposure, outcome, and query under 300 characters each. Return at most three short inclusion criteria, three short exclusion criteria, and three short limitations. Do not add output-specific tables or narrative sections.",
        json.dumps(
            {
                "task": "Create an editable PICO or PEO literature-review protocol",
                "question": question,
                "requested_outputs": outputs,
                "required_schema": {
                    "framework": "PICO or PEO",
                    "population": "string",
                    "exposure": "string",
                    "outcome": "string",
                    "query": "Boolean search query",
                    "inclusion_criteria": ["string"],
                    "exclusion_criteria": ["string"],
                    "limitations": ["string"],
                },
            },
            ensure_ascii=False,
        ),
        360,
    )
    required = ("framework", "population", "exposure", "outcome", "query")
    if any(not str(result.get(key, "")).strip() for key in required):
        raise ModelError("Model protocol is missing required fields")
    result.update({"outputs": outputs, "generated_by": "llm_supervisor", "editable": True, "source_citations": []})
    return result


def agent_reply(message, task):
    return complete(
        "You are the Supervisor of a scientific evidence agent. Help the user refine the research protocol and decide the next auditable action. Never claim that a paper was searched, screened, extracted, or verified unless the supplied task state contains that evidence. Clearly separate model advice from MCP results and human confirmations. Reply in concise Chinese plain text without Markdown formatting characters.",
        json.dumps({"user_message": message, "task_state": task}, ensure_ascii=False),
        1000,
    )
