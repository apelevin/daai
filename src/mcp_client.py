"""Synchronous MCP client over SSE transport.

Uses only stdlib (urllib, threading, queue) — no extra dependencies.
Implements JSON-RPC 2.0 over MCP SSE transport.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.request
from typing import Any

from src.config import MCP_SSE_URL, MCP_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_MSG_ID_SEQ = 0
_MSG_ID_LOCK = threading.Lock()


def _next_id() -> int:
    global _MSG_ID_SEQ
    with _MSG_ID_LOCK:
        _MSG_ID_SEQ += 1
        return _MSG_ID_SEQ


class MCPError(Exception):
    pass


class MCPClient:
    """Single-use MCP client.  Call close() when done (or use call_mcp_once)."""

    def __init__(self, sse_url: str = MCP_SSE_URL, timeout: int = MCP_TIMEOUT_SECONDS):
        self._sse_url = sse_url
        self._timeout = timeout
        self._messages_url: str | None = None
        self._response_queue: queue.Queue = queue.Queue()
        self._sse_thread: threading.Thread | None = None
        self._closed = False
        self._connect()

    # ── connection ───────────────────────────────────────────────────────

    def _connect(self) -> None:
        """Open SSE stream and wait for the server to advertise its messages URL."""
        endpoint_ready = threading.Event()
        endpoint_error: list[str] = []

        def _sse_reader():
            try:
                req = urllib.request.Request(self._sse_url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    event_type = ""
                    data_lines: list[str] = []
                    for raw in resp:
                        if self._closed:
                            break
                        line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                        elif line == "":
                            data = "\n".join(data_lines)
                            self._handle_sse_event(event_type, data, endpoint_ready)
                            event_type = ""
                            data_lines = []
            except Exception as e:
                if not endpoint_ready.is_set():
                    endpoint_error.append(str(e))
                    endpoint_ready.set()
                elif not self._closed:
                    logger.warning("SSE stream error: %s", e)

        self._sse_thread = threading.Thread(target=_sse_reader, daemon=True)
        self._sse_thread.start()

        if not endpoint_ready.wait(timeout=self._timeout):
            self._closed = True
            raise MCPError(f"MCP server did not send endpoint within {self._timeout}s")
        if endpoint_error:
            self._closed = True
            raise MCPError(f"SSE connection failed: {endpoint_error[0]}")

    def _handle_sse_event(self, event_type: str, data: str, endpoint_ready: threading.Event) -> None:
        if event_type == "endpoint":
            # data is the relative or absolute messages URL
            url = data.strip()
            if url.startswith("/"):
                # Make absolute using SSE base
                from urllib.parse import urlparse
                parsed = urlparse(self._sse_url)
                url = f"{parsed.scheme}://{parsed.netloc}{url}"
            self._messages_url = url
            endpoint_ready.set()
            logger.debug("MCP messages URL: %s", url)
        elif event_type == "message" or event_type == "":
            try:
                msg = json.loads(data)
                self._response_queue.put(msg)
            except json.JSONDecodeError:
                pass

    # ── JSON-RPC transport ───────────────────────────────────────────────

    def _call(self, method: str, params: dict) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        if not self._messages_url:
            raise MCPError("Not connected (no messages URL)")

        msg_id = _next_id()
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }).encode("utf-8")

        req = urllib.request.Request(
            self._messages_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            _ = resp.read()  # POST body is typically empty / 202 accepted

        # Wait for the matching response from SSE stream
        deadline = self._timeout
        while deadline > 0:
            try:
                msg = self._response_queue.get(timeout=1)
                if msg.get("id") == msg_id:
                    if "error" in msg:
                        raise MCPError(f"MCP error: {msg['error']}")
                    return msg.get("result")
                else:
                    # Different id — put back for another caller (unlikely in sync mode)
                    self._response_queue.put(msg)
                    deadline -= 1
            except queue.Empty:
                deadline -= 1

        raise MCPError(f"Timeout waiting for response to {method} (id={msg_id})")

    # ── MCP tool wrappers ─────────────────────────────────────────────────

    def initialize(self) -> dict:
        """MCP handshake — call once before other methods."""
        result = self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "daai", "version": "1.0"},
        })
        # Send initialized notification (no response expected)
        try:
            payload = json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }).encode("utf-8")
            req = urllib.request.Request(
                self._messages_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                r.read()
        except Exception:
            pass
        return result or {}

    def get_object_details(self, schema: str, object_name: str, object_type: str = "table") -> dict:
        """Get detailed column info for a specific table/view."""
        result = self._call("tools/call", {
            "name": "get_object_details",
            "arguments": {"schema_name": schema, "object_name": object_name, "object_type": object_type},
        })
        rows = _parse_tool_result(result)
        return rows[0] if rows else {}

    def list_objects(self, schema: str = "ai_bi") -> list[dict]:
        """List tables and columns in the given schema.

        Returns list of dicts: [{table, description, columns: [{name, type, description}]}]
        """
        result = self._call("tools/call", {
            "name": "list_objects",
            "arguments": {"schema_name": schema},
        })
        return _parse_tool_result(result)

    def execute_sql(self, sql: str) -> list[dict]:
        """Execute a SQL query and return rows as list of dicts."""
        result = self._call("tools/call", {
            "name": "execute_sql",
            "arguments": {"sql": sql},
        })
        return _parse_tool_result(result)

    def close(self) -> None:
        self._closed = True


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_tool_result(result: Any) -> list:
    """Extract content from MCP tool result."""
    if result is None:
        return []
    # MCP tools/call returns {content: [{type: "text", text: "..."}]}
    if isinstance(result, dict) and "content" in result:
        for item in result["content"]:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except (json.JSONDecodeError, KeyError):
                    return [{"raw": item.get("text", "")}]
    if isinstance(result, list):
        return result
    return [{"raw": str(result)}]


def call_mcp_once(fn_name: str, **kwargs) -> list:
    """Create a throw-away MCPClient, call one method, close.  Returns parsed result."""
    client = MCPClient()
    try:
        client.initialize()
        method = getattr(client, fn_name)
        return method(**kwargs)
    finally:
        client.close()
