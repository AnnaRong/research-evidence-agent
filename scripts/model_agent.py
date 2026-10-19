#!/usr/bin/env python3
"""Minimal server-side LLM adapter for protocol generation and agent chat."""

import json
import os
import random
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelError(RuntimeError):
    def __init__(self, message, category="model_error", retryable=False, status=None):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status = status


def model_configs():
    """Return configured providers in priority order; the first is primary."""
    configs = []
    gateway_key = os.environ.get("LLM_API_KEY", "").strip()
    gateway_enabled = os.environ.get("LLM_EGRESS_ENABLED", "0") == "1"
    if gateway_key and gateway_enabled:
        base_url = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
        allowed_host = os.environ.get("LLM_ALLOWED_HOST", "").strip()
        if base_url.startswith("https://") and (not allowed_host or base_url.split("/", 3)[2] == allowed_host):
            configs.append({
                "provider": "openai-compatible",
                "base_url": base_url,
                "api_key": gateway_key,
                "model": os.environ.get("LLM_MODEL", "gpt-5-mini"),
                "endpoint": "chat/completions",
            })
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        configs.append({
            "provider": "anthropic",
            "base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/"),
            "api_key": os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            "endpoint": "messages",
        })
    if os.environ.get("OPENAI_API_KEY"):
        configs.append({
            "provider": "openai",
            "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            "api_key": os.environ["OPENAI_API_KEY"],
            "model": os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
            "endpoint": "responses",
        })
    local_url = os.environ.get("LOCAL_LLM_BASE_URL", "").strip().rstrip("/")
    if local_url and local_url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
        configs.append({
            "provider": "local-openai-compatible",
            "base_url": local_url,
            "api_key": os.environ.get("LOCAL_LLM_API_KEY", "local"),
            "model": os.environ.get("LOCAL_LLM_MODEL", "local-model"),
            "endpoint": "chat/completions",
        })
    return configs


def model_config():
    configs = model_configs()
    return configs[0] if configs else None


def model_status():
    config = model_config()
    return {
        "configured": bool(config),
        "provider": config["provider"] if config else None,
        "model": config["model"] if config else None,
    }


def available_models():
    return [{"provider": row["provider"], "model": row["model"], "primary": index == 0} for index, row in enumerate(model_configs())]


def _request(url, headers, payload, timeout=None):
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout or float(os.environ.get("LLM_TIMEOUT_SECONDS", "90"))) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        retryable = exc.code in {408, 409, 429, 500, 502, 503, 504}
        category = "rate_limit" if exc.code == 429 else "auth" if exc.code in {401, 403} else "model_http"
        raise ModelError("Model API returned {}: {}".format(exc.code, detail), category, retryable, exc.code) from exc
    except (URLError, TimeoutError) as exc:
        raise ModelError("Model API request failed: {}".format(exc), "timeout", True) from exc
    except json.JSONDecodeError as exc:
        raise ModelError("Model API returned invalid JSON", "parse_error", True) from exc


def _usage(data):
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": int(usage.get("total_tokens", input_tokens + output_tokens) or input_tokens + output_tokens)}


def _invoke(config, system, user, max_tokens, timeout=None):
    if config["provider"] in {"openai-compatible", "local-openai-compatible"}:
        payload = {"model": config["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_completion_tokens": max_tokens}
        if "json" in system.lower() or "json" in user.lower():
            payload["response_format"] = {"type": "json_object"}
        data = _request(
            config["base_url"] + "/chat/completions",
            {"Content-Type": "application/json", "Authorization": "Bearer " + config["api_key"]},
            payload, timeout,
        )
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ModelError("OpenAI-compatible gateway returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
        text = str(content).strip()
    elif config["provider"] == "anthropic":
        data = _request(
            config["base_url"] + "/v1/messages",
            {
                "Content-Type": "application/json",
                "x-api-key": config["api_key"],
                "Authorization": "Bearer " + config["api_key"],
                "anthropic-version": "2023-06-01",
            },
            {"model": config["model"], "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}, timeout,
        )
        text = "".join(item.get("text", "") for item in data.get("content", []) if item.get("type") == "text").strip()
    else:
        data = _request(
            config["base_url"] + "/responses",
            {"Content-Type": "application/json", "Authorization": "Bearer " + config["api_key"]},
            {"model": config["model"], "instructions": system, "input": user, "max_output_tokens": max_tokens}, timeout,
        )
        text = (data.get("output_text") or "".join(part.get("text", "") for item in data.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text")).strip()
    usage = _usage(data)
    input_price = float(os.environ.get("LLM_INPUT_USD_PER_1M", "0") or 0)
    output_price = float(os.environ.get("LLM_OUTPUT_USD_PER_1M", "0") or 0)
    usage["estimated_cost_usd"] = round((usage["input_tokens"] * input_price + usage["output_tokens"] * output_price) / 1_000_000, 8)
    return {"text": text, "provider": config["provider"], "model": config["model"], "usage": usage}


def complete_result(system, user, max_tokens=1400, timeout=None, retries=None):
    configs = model_configs()
    if not configs:
        raise ModelError("No model API key configured", "not_configured", False)
    retries = max(0, int(os.environ.get("LLM_MAX_RETRIES", "2") if retries is None else retries))
    failures = []
    for config in configs:
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                result = _invoke(config, system, user, max_tokens, timeout)
                result.update({"attempts": attempt + 1, "fallback_count": len(failures), "duration_ms": int((time.monotonic() - started) * 1000)})
                return result
            except ModelError as exc:
                failures.append({"provider": config["provider"], "category": exc.category, "message": str(exc)})
                if not exc.retryable or attempt == retries:
                    break
                time.sleep(min(4.0, 0.25 * (2 ** attempt)) + random.random() * 0.1)
    last = failures[-1]
    raise ModelError("All configured models failed; last error: {}".format(last["message"]), last["category"], False)


def complete(system, user, max_tokens=1400):
    return complete_result(system, user, max_tokens)["text"]


def validate_json_schema(value, schema, path="$"):
    types = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int, "boolean": bool}
    expected = schema.get("type")
    if expected in types and not isinstance(value, types[expected]):
        raise ModelError("Structured output {} must be {}".format(path, expected), "schema_validation", False)
    if expected == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise ModelError("Structured output {} is missing {}".format(path, key), "schema_validation", False)
        for key, child in schema.get("properties", {}).items():
            if key in value:
                validate_json_schema(value[key], child, path + "." + key)
    if expected == "array" and schema.get("items"):
        for index, child in enumerate(value):
            validate_json_schema(child, schema["items"], "{}[{}]".format(path, index))


def complete_json(system, user, max_tokens=1400, schema=None, return_metadata=False):
    response = complete_result(system + "\nReturn one valid JSON object only. Do not use Markdown fences.", user, max_tokens)
    text = response["text"]
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ModelError("Model did not return a JSON object")
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ModelError("Model returned invalid JSON", "parse_error", False) from exc
    if not isinstance(value, dict):
        raise ModelError("Model did not return a JSON object", "schema_validation", False)
    if schema:
        validate_json_schema(value, schema)
    return {"value": value, **{key: response[key] for key in response if key != "text"}} if return_metadata else value


def generate_protocol_with_model(question, outputs):
    schema = {
        "type": "object",
        "required": ["framework", "population", "exposure", "outcome", "query", "inclusion_criteria", "exclusion_criteria", "limitations"],
        "properties": {
            "framework": {"type": "string"}, "population": {"type": "string"}, "exposure": {"type": "string"},
            "outcome": {"type": "string"}, "query": {"type": "string"},
            "inclusion_criteria": {"type": "array", "items": {"type": "string"}},
            "exclusion_criteria": {"type": "array", "items": {"type": "string"}},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
    }
    response = complete_json(
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
        360, schema=schema, return_metadata=True,
    )
    result = response["value"]
    required = ("framework", "population", "exposure", "outcome", "query")
    if any(not str(result.get(key, "")).strip() for key in required):
        raise ModelError("Model protocol is missing required fields")
    result.update({"outputs": outputs, "generated_by": "llm_supervisor", "editable": True, "source_citations": [], "model_run": {key: response[key] for key in ("provider", "model", "usage", "attempts", "fallback_count", "duration_ms")}})
    return result


def agent_reply(message, task):
    return complete(
        "You are the Supervisor of a scientific evidence agent. Help the user refine the research protocol and decide the next auditable action. Never claim that a paper was searched, screened, extracted, or verified unless the supplied task state contains that evidence. Clearly separate model advice from MCP results and human confirmations. Reply in concise Chinese plain text without Markdown formatting characters, using no more than 250 Chinese characters.",
        json.dumps({"user_message": message, "task_state": task}, ensure_ascii=False),
        360,
    )
