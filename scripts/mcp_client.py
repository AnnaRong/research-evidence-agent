#!/usr/bin/env python3
"""Small dependency-free MCP client for the public agent runtime.

The client intentionally keeps the MCP boundary narrow: initialize a server,
call a named tool, return the raw result, and never invent evidence.
"""

import json
import os
import selectors
import subprocess
import time
import uuid
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class MCPError(RuntimeError):
    def __init__(self, message, category="mcp_error", retryable=True):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


def _parse_response(data, content_type=""):
    if not data:
        return {}
    text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
    if "text/event-stream" in content_type or text.lstrip().startswith("event:"):
        candidates = []
        for line in text.splitlines():
            if line.startswith("data:"):
                candidates.append(line[5:].strip())
        text = candidates[-1] if candidates else ""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPError("MCP returned non-JSON output: {}".format(text[:400]), "parse_error", False) from exc


def _rpc_error(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if not error:
        return None
    message = error.get("message", "MCP JSON-RPC error") if isinstance(error, dict) else str(error)
    code = error.get("code") if isinstance(error, dict) else None
    category = "rate_limit" if code == 429 or "rate" in message.lower() or "quota" in message.lower() else "mcp_error"
    raise MCPError(message, category, category == "rate_limit")


def _result_text(result):
    if isinstance(result, dict):
        content = result.get("content") or []
        parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        if parts:
            return "\n".join(parts)
        for key in ("structuredContent", "data", "result"):
            if key in result:
                return json.dumps(result[key], ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


class StreamableHTTPMCP:
    def __init__(self, url, token=None, timeout=90):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session_id = None
        self._next_id = 0

    def _call(self, method, params=None, notification=False):
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            payload["id"] = self._next_id
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                body = _parse_response(response.read(), response.headers.get("Content-Type", ""))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:700]
            category = "rate_limit" if exc.code == 429 else "auth" if exc.code in {401, 403} else "mcp_http"
            raise MCPError("MCP HTTP {}: {}".format(exc.code, detail), category, exc.code in {408, 429, 500, 502, 503, 504}) from exc
        except (URLError, TimeoutError) as exc:
            raise MCPError("MCP HTTP request failed: {}".format(exc), "timeout", True) from exc
        if notification:
            return {}
        _rpc_error(body)
        return body.get("result", body)

    def call(self, tool, arguments):
        self._call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "research-evidence-agent", "version": "1.0"}})
        self._call("notifications/initialized", {}, notification=True)
        result = self._call("tools/call", {"name": tool, "arguments": arguments})
        return {"server": "elicit", "tool": tool, "result": result, "text": _result_text(result), "session_id": self.session_id}


class StdioMCP:
    def __init__(self, command=None, args=None, env=None, timeout=120):
        self.command = command or os.environ.get("EDISON_MCP_COMMAND", "uvx")
        self.args = args or os.environ.get("EDISON_MCP_ARGS", "edison-mcp@latest stdio").split()
        self.env = dict(os.environ)
        if env:
            self.env.update(env)
        self.timeout = timeout

    def _read_message(self, process):
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            ready = selector.select(max(0.1, deadline - time.time()))
            if not ready:
                continue
            line = process.stdout.readline()
            if not line:
                break
            line = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
            if line.strip():
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        raise MCPError("MCP stdio response timed out", "timeout", True)

    def call(self, tool, arguments):
        process = subprocess.Popen([self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        try:
            def send(method, params=None, request_id=None):
                payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
                if request_id is not None:
                    payload["id"] = request_id
                process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                process.stdin.flush()
                return self._read_message(process) if request_id is not None else {}

            send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "research-evidence-agent", "version": "1.0"}}, 1)
            send("notifications/initialized", {})
            result = send("tools/call", {"name": tool, "arguments": arguments}, 2)
            _rpc_error(result)
            return {"server": "edison", "tool": tool, "result": result.get("result", result), "text": _result_text(result.get("result", result)), "session_id": None}
        except FileNotFoundError as exc:
            raise MCPError("MCP command not found: {}".format(self.command), "not_configured", False) from exc
        finally:
            try:
                process.kill()
            except ProcessLookupError:
                pass


def mcp_config():
    return {
        "elicit": bool(os.environ.get("ELICIT_MCP_TOKEN") or os.environ.get("ELICIT_API_KEY")),
        "edison": bool(os.environ.get("EDISON_API_KEY")),
        "elicit_url": os.environ.get("ELICIT_MCP_URL", "https://elicit.com/api/mcp"),
    }


def call_tool(server, tool, arguments):
    if server == "elicit":
        token = os.environ.get("ELICIT_MCP_TOKEN") or os.environ.get("ELICIT_API_KEY")
        if not token:
            raise MCPError("ELICIT_MCP_TOKEN or ELICIT_API_KEY is not configured", "not_configured", False)
        return StreamableHTTPMCP(os.environ.get("ELICIT_MCP_URL", "https://elicit.com/api/mcp"), token=token).call(tool, arguments)
    if server == "edison":
        key = os.environ.get("EDISON_API_KEY")
        if not key:
            raise MCPError("EDISON_API_KEY is not configured", "not_configured", False)
        return StdioMCP(env={"EDISON_API_KEY": key}).call(tool, arguments)
    raise MCPError("Unsupported MCP server: {}".format(server), "not_configured", False)
