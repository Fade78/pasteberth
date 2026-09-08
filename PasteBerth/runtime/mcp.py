"""Minimal standard-library MCP stdio transport.

The transport deliberately knows nothing about Pasteberth storage.  The CLI
supplies the domain callback for the ``drop`` tool so the MCP surface remains
an adapter over the existing HTTP client and service contract.
"""
from __future__ import annotations

import io
import json
import math
import sys
from collections.abc import Callable
from typing import Any, BinaryIO, TextIO

from . import __version__


MCP_PROTOCOL_VERSION = "2025-06-18"
MODERN_PROTOCOL_VERSION = "2026-07-28"
MODERN_PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MODERN_DISCOVERY_TTL_MS = 3_600_000
MODERN_TOOL_LIST_TTL_MS = 300_000
_LEGACY_PROTOCOL_VERSIONS = (
    "2025-11-25",
    MCP_PROTOCOL_VERSION,
    "2025-03-26",
    "2024-11-05",
)
_SUPPORTED_PROTOCOL_VERSIONS = set(_LEGACY_PROTOCOL_VERSIONS)


class McpToolError(Exception):
    """A user-facing validation or execution error from an MCP tool."""


class UnsupportedProtocolVersionError(Exception):
    """A modern MCP request declared a protocol version we do not support."""

    def __init__(self, requested: object) -> None:
        super().__init__(f"unsupported protocol version: {requested!r}")
        self.requested = requested


DROP_TOOL = {
    "name": "drop",
    "description": (
        "Drop one or more files or UTF-8 contents into a configured Pasteberth zone. "
        "Use path for an existing local file, or content with filename for text."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["zone", "items"],
        "properties": {
            "zone": {
                "type": "string",
                "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
                "description": "Configured zone ID.",
            },
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "oneOf": [
                        {
                            "required": ["path"],
                            "not": {
                                "anyOf": [
                                    {"required": ["content"]},
                                    {"required": ["content_base64"]},
                                    {"required": ["filename"]},
                                ]
                            },
                        },
                        {
                            "required": ["content", "filename"],
                            "not": {
                                "anyOf": [
                                    {"required": ["path"]},
                                    {"required": ["content_base64"]},
                                ]
                            },
                        },
                        {
                            "required": ["content_base64", "filename"],
                            "not": {
                                "anyOf": [
                                    {"required": ["path"]},
                                    {"required": ["content"]},
                                ]
                            },
                        },
                    ],
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 4096,
                            "description": "Path to a local regular file.",
                        },
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "description": "UTF-8 text to drop.",
                        },
                        "content_base64": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Binary content encoded as base64.",
                        },
                        "filename": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "Managed filename for in-memory content.",
                        },
                        "mime": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 120,
                            "description": "Optional declared MIME type.",
                        },
                    },
                },
            },
            "replace": {
                "type": "boolean",
                "default": False,
                "description": "Allow replacement of an existing managed filename.",
            },
        },
    },
}


class McpServer:
    """Handle JSON-RPC messages for the small Pasteberth MCP surface."""

    def __init__(
        self,
        drop_handler: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        server_name: str = "pasteberth",
        server_version: str = __version__,
    ) -> None:
        self._drop_handler = drop_handler
        self._server_name = server_name
        self._server_version = server_version

    def handle(self, message: object) -> dict[str, Any] | None:
        """Return one JSON-RPC response, or ``None`` for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "invalid request")

        has_id = "id" in message
        request_id = message.get("id")
        if has_id and (
            isinstance(request_id, bool)
            or not isinstance(request_id, (str, int, float))
            or (isinstance(request_id, float) and not math.isfinite(request_id))
        ):
            return self._error(None, -32600, "invalid request")

        method = message.get("method")
        if not isinstance(method, str):
            return self._error(request_id if has_id else None, -32600, "invalid request")

        try:
            modern_version = self._modern_request_version(message)
            result = self._dispatch(method, message.get("params"))
            if modern_version:
                result = self._modern_result(result, method)
        except UnsupportedProtocolVersionError as exc:
            response = self._error(
                request_id if has_id else None,
                -32022,
                "Unsupported protocol version",
                data={
                    "supported": [MODERN_PROTOCOL_VERSION],
                    "requested": exc.requested,
                },
            )
            return response if has_id else None
        except ValueError as exc:
            response = self._error(
                request_id if has_id else None,
                -32602,
                str(exc),
            )
            return response if has_id else None
        except LookupError as exc:
            response = self._error(
                request_id if has_id else None,
                -32601,
                str(exc),
            )
            return response if has_id else None
        except Exception:
            # A tool failure is returned as an MCP tool result.  An unexpected
            # protocol/server failure remains a JSON-RPC internal error.
            response = self._error(
                request_id if has_id else None,
                -32603,
                "internal error",
            )
            return response if has_id else None

        return {"jsonrpc": "2.0", "id": request_id, "result": result} if has_id else None

    def _dispatch(self, method: str, params: object) -> dict[str, Any]:
        if method == "server/discover":
            if params is not None and not isinstance(params, dict):
                raise ValueError("server/discover params must be an object")
            return {
                "resultType": "complete",
                "supportedVersions": [
                    MODERN_PROTOCOL_VERSION,
                    *_LEGACY_PROTOCOL_VERSIONS,
                ],
                "capabilities": {"tools": {}},
                "_meta": {
                    SERVER_INFO_META: {
                        "name": self._server_name,
                        "version": self._server_version,
                    }
                },
                "instructions": "Use the drop tool to put content into a Pasteberth zone.",
            }
        if method == "initialize":
            if not isinstance(params, dict):
                raise ValueError("initialize params must be an object")
            requested = params.get("protocolVersion")
            protocol_version = (
                requested
                if isinstance(requested, str) and requested in _SUPPORTED_PROTOCOL_VERSIONS
                else MCP_PROTOCOL_VERSION
            )
            return {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": self._server_name,
                    "version": self._server_version,
                },
            }
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            if params is not None and not isinstance(params, dict):
                raise ValueError("tools/list params must be an object")
            return {"tools": [DROP_TOOL]}
        if method == "tools/call":
            return self._call_tool(params)
        raise LookupError(f"method not found: {method}")

    @staticmethod
    def _modern_request_version(message: dict[str, Any]) -> str | None:
        method = message.get("method")
        params = message.get("params")
        if method == "server/discover":
            if not isinstance(params, dict):
                return MODERN_PROTOCOL_VERSION
        elif not isinstance(params, dict):
            return None
        metadata = params.get("_meta")
        if not isinstance(metadata, dict) or MODERN_PROTOCOL_META not in metadata:
            return MODERN_PROTOCOL_VERSION if method == "server/discover" else None
        version = metadata[MODERN_PROTOCOL_META]
        if version != MODERN_PROTOCOL_VERSION:
            raise UnsupportedProtocolVersionError(version)
        return version

    def _modern_result(self, result: dict[str, Any], method: str) -> dict[str, Any]:
        enriched = dict(result)
        enriched.setdefault("resultType", "complete")
        metadata = dict(enriched.get("_meta") or {})
        metadata.setdefault(
            SERVER_INFO_META,
            {"name": self._server_name, "version": self._server_version},
        )
        enriched["_meta"] = metadata
        if method == "server/discover":
            enriched.setdefault("ttlMs", MODERN_DISCOVERY_TTL_MS)
            enriched.setdefault("cacheScope", "public")
        elif method == "tools/list":
            enriched.setdefault("ttlMs", MODERN_TOOL_LIST_TTL_MS)
            enriched.setdefault("cacheScope", "public")
        return enriched

    def _call_tool(self, params: object) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("tools/call params must be an object")
        if params.get("name") != "drop":
            raise ValueError(f"unknown tool: {params.get('name')!r}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        try:
            result = self._drop_handler(arguments)
        except McpToolError as exc:
            return self._tool_error(str(exc))
        except Exception:
            return self._tool_error("drop failed")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            "isError": bool(result.get("errors")),
        }

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        }

    @staticmethod
    def _error(
        request_id: object,
        code: int,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        if data is not None:
            error["error"]["data"] = data
        return error


def run_stdio(
    drop_handler: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    stdin: TextIO | BinaryIO | None = None,
    stdout: TextIO | BinaryIO | None = None,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> int:
    """Run newline-delimited JSON-RPC on stdin/stdout."""
    if max_message_bytes <= 0:
        raise ValueError("max_message_bytes must be positive")
    input_stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
    output_stream = stdout if stdout is not None else getattr(sys.stdout, "buffer", sys.stdout)
    server = McpServer(drop_handler)
    while True:
        raw_line = input_stream.readline(max_message_bytes + 1)
        if not raw_line:
            break
        if len(raw_line) > max_message_bytes:
            if not _line_ended(raw_line):
                while True:
                    remainder = input_stream.readline(8192)
                    if not remainder or _line_ended(remainder):
                        break
            _write_response(output_stream, McpServer._error(None, -32600, "message too large"))
            continue
        if isinstance(raw_line, bytes):
            try:
                raw_line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                _write_response(output_stream, McpServer._error(None, -32700, "parse error"))
                continue
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            response = server._error(None, -32700, "parse error")
        else:
            response = server.handle(message)
        if response is not None:
            _write_response(output_stream, response)
    return 0


def _write_response(stream: TextIO | BinaryIO, response: dict[str, Any]) -> None:
    text = json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
    writer: Any = stream
    if isinstance(stream, io.TextIOBase):
        writer.write(text)
    else:
        writer.write(text.encode("utf-8"))
    writer.flush()


def _line_ended(value: str | bytes) -> bool:
    return value.endswith(b"\n") if isinstance(value, bytes) else value.endswith("\n")
