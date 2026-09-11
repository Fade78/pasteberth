"""Tests for the optional MCP stdio adapter and its HTTP-backed drop tool."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PasteBerth.runtime.mcp import McpServer, run_stdio
from PasteBerth.runtime.tokens import PERMISSION_WRITE, SCOPE_ZONE, TokenGrant
from tests.helpers import LiveServer, REPO_ROOT, write_config


ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}


def _json_line(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _run_mcp(config: Path, port: int, messages: list[dict], *, env: dict | None = None):
    process_env = dict(ENV)
    process_env.pop("PASTEBERTH_PASSWORD", None)
    process_env.update(env or {})
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "PasteBerth.runtime",
            "mcp",
            "--config",
            str(config),
            "--server",
            f"http://127.0.0.1:{port}",
        ],
        input="\n".join(_json_line(message) for message in messages) + "\n",
        capture_output=True,
        text=True,
        env=process_env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )


class TestMcpProtocol(unittest.TestCase):
    def test_initialize_tools_and_notification(self):
        output = io.StringIO()
        messages = "\n".join(
            (
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26"},
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "drop",
                            "arguments": {"zone": "default", "items": []},
                        },
                    }
                ),
            )
        )

        calls = []

        def drop(arguments):
            calls.append(arguments)
            return {"zone": "default", "items": [], "errors": []}

        result = run_stdio(drop, stdin=io.StringIO(messages + "\n"), stdout=output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]

        self.assertEqual(result, 0)
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(responses[1]["result"]["tools"][0]["name"], "drop")
        self.assertEqual(responses[2]["result"]["isError"], False)
        self.assertEqual(calls, [{"zone": "default", "items": []}])

    def test_unknown_method_is_method_not_found(self):
        response = McpServer(lambda _arguments: {}).handle(
            {"jsonrpc": "2.0", "id": 4, "method": "unknown"}
        )

        if response is None:
            self.fail("expected a JSON-RPC response")
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_tool_is_invalid_params(self):
        response = McpServer(lambda _arguments: {}).handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "other", "arguments": {}},
            }
        )

        self.assertEqual(response["error"]["code"], -32602)

    def test_modern_discovery_and_request_metadata(self):
        calls = []

        def drop(arguments):
            calls.append(arguments)
            return {"zone": "default", "items": [], "errors": []}

        server = McpServer(drop)
        discover = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}
        )
        self.assertEqual(discover["result"]["resultType"], "complete")
        self.assertEqual(discover["result"]["supportedVersions"][0], "2026-07-28")
        self.assertEqual(discover["result"]["cacheScope"], "public")
        self.assertIn("io.modelcontextprotocol/serverInfo", discover["result"]["_meta"])

        tools = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
            }
        )
        self.assertEqual(tools["result"]["resultType"], "complete")
        self.assertEqual(tools["result"]["cacheScope"], "public")
        self.assertIn("io.modelcontextprotocol/serverInfo", tools["result"]["_meta"])

        called = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "drop",
                    "arguments": {"zone": "default", "items": []},
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
                },
            }
        )
        self.assertEqual(called["result"]["resultType"], "complete")
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(calls, [{"zone": "default", "items": []}])

    def test_modern_request_rejects_an_unsupported_version(self):
        response = McpServer(lambda _arguments: {}).handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "1900-01-01"
                    }
                },
            }
        )

        self.assertEqual(response["error"]["code"], -32022)
        self.assertEqual(response["error"]["data"]["requested"], "1900-01-01")
        self.assertEqual(response["error"]["data"]["supported"], ["2026-07-28"])

    def test_stdio_uses_utf8_bytes_and_rejects_oversized_messages(self):
        output = io.BytesIO()
        request = _json_line(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "drop", "arguments": {"text": "café"}},
            }
        ).encode("utf-8") + b"\n"

        run_stdio(
            lambda arguments: {"text": arguments["text"], "errors": []},
            stdin=io.BytesIO(request),
            stdout=output,
            max_message_bytes=len(request),
        )

        response = json.loads(output.getvalue())
        text = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(text["text"], "café")

        oversized = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        output = io.StringIO()
        run_stdio(lambda _arguments: {}, stdin=oversized, stdout=output, max_message_bytes=8)
        self.assertEqual(json.loads(output.getvalue())["error"]["message"], "message too large")


class TestMcpDrop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = write_config(self.tmp)
        self.server = LiveServer(self.cfg)
        self.addCleanup(self.server.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_drops_paths_and_in_memory_content_through_http(self):
        source = self.tmp / "source.bin"
        source.write_bytes(b"from path")
        proc = _run_mcp(
            self.cfg,
            self.server.port,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "drop",
                        "arguments": {
                            "zone": "default",
                            "items": [
                                {"path": str(source)},
                                {
                                    "filename": "notes.txt",
                                    "content": "from memory",
                                },
                                {
                                    "filename": "blob.bin",
                                    "content_base64": "AAFi",
                                },
                            ],
                        },
                    },
                }
            ],
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        response = json.loads(proc.stdout)
        result = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(
            (self.tmp / "default-images" / "source.bin").read_bytes(),
            b"from path",
        )
        self.assertEqual(
            (self.tmp / "default-images" / "notes.txt").read_text(),
            "from memory",
        )
        self.assertEqual(
            (self.tmp / "default-images" / "blob.bin").read_bytes(),
            b"\x00\x01b",
        )
        self.assertEqual(
            [item["filename"] for item in result["items"]],
            ["source.bin", "notes.txt", "blob.bin"],
        )
        self.assertEqual(result["errors"], [])

    def test_authenticated_drop_uses_environment_password(self):
        auth_tmp = self.tmp / "auth"
        auth_tmp.mkdir(mode=0o700)
        cfg = write_config(auth_tmp, auth_enabled=True, password="mcp-password")
        server = LiveServer(cfg)
        self.addCleanup(server.stop)

        proc = _run_mcp(
            cfg,
            server.port,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "drop",
                        "arguments": {
                            "zone": "default",
                            "items": [
                                {"filename": "secure.txt", "content": "secret"}
                            ],
                        },
                    },
                }
            ],
            env={"PASTEBERTH_PASSWORD": "mcp-password"},
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        response = json.loads(proc.stdout)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(
            (auth_tmp / "default-images" / "secure.txt").read_text(),
            "secret",
        )

    def test_authenticated_drop_accepts_write_only_bearer_response(self):
        auth_tmp = self.tmp / "bearer-auth"
        auth_tmp.mkdir(mode=0o700)
        cfg = write_config(auth_tmp, auth_enabled=True, password="unused-password")
        server = LiveServer(cfg)
        self.addCleanup(server.stop)
        _record, token = server.tokens.create(
            "mcp",
            [TokenGrant(SCOPE_ZONE, "default", PERMISSION_WRITE)],
            duration_seconds=3600,
        )

        proc = _run_mcp(
            cfg,
            server.port,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "drop",
                        "arguments": {
                            "zone": "default",
                            "items": [{"filename": "bearer.txt", "content": "secret"}],
                        },
                    },
                }
            ],
            env={"PASTEBERTH_TOKEN": token},
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        response = json.loads(proc.stdout)
        result = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(result["items"], [{"accepted": True}])
        self.assertEqual(
            (auth_tmp / "default-images" / "bearer.txt").read_text(),
            "secret",
        )
