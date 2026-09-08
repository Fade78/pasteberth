# MCP Reference

[Documentation map](../../GUIDE.md).

## MCP stdio adapter

```text
pasteberth mcp [--config PATH] [--server URL] [--insecure]
```

```sh
# Have the trusted launcher supply PASTEBERTH_PASSWORD through its environment.
pasteberth mcp --config /absolute/path/config.toml
```

On Windows, set `PASTEBERTH_PASSWORD` in `cmd.exe` before invoking
`PasteBerth\pasteberth.cmd`; in PowerShell use `$env:PASTEBERTH_PASSWORD =
"your-password"`. The MCP process can read any regular local file readable by
its account, so it should only be launched by a trusted agent.

`mcp` serves newline-delimited JSON-RPC on standard input and standard output.
The initial `drop` tool accepts a `zone` and one or more `items`; each item is
either a local `path`, UTF-8 `content` plus `filename`, or `content_base64` plus
`filename`. It calls the existing HTTP upload endpoint rather than accessing
Pasteberth storage directly. `PASTEBERTH_PASSWORD` is used after a `401`; the
adapter never prompts on stdin because that stream belongs to MCP. The adapter
supports modern `server/discover` and per-request metadata for protocol
`2026-07-28`, and the legacy `initialize` handshake described below.

The adapter is an optional local stdio process, not an HTTP MCP server or a
storage daemon. Start the Pasteberth HTTP service separately. Its server URL
comes from configuration, defaults to `http://127.0.0.1:8765` without a config,
or is overridden by `--server URL`. Include the public mount path when needed,
for example `https://pasteberth.example.internal/paste`.

Certificate verification is enabled by default. `--insecure` disables it only;
use that exception solely for a separately trusted self-signed endpoint. Prefer
a trusted certificate and a matching hostname. The Windows launcher examples
describe available tooling, not an official native-platform support promise;
see [support](../deployment.md#requirements-and-support).

## Drop Tool

`tools/list` exposes one tool, `drop`. Its arguments are:

| Field | Required | Meaning |
|---|---|---|
| `zone` | yes | Zone ID matching `^[a-z0-9][a-z0-9_-]{0,63}$`; not a directory or collection ID. |
| `items` | yes | Nonempty array of at most 128 items. |
| `replace` | no | Boolean, default `false`; permits replacement only of a coherent managed name. |

Each item has exactly one source form:

| Source Form | Fields | Filename |
|---|---|---|
| Local file | `path`, optional `mime` | Basename of the local regular file; a separate `filename` is invalid. |
| UTF-8 text | `content`, `filename`, optional `mime` | Explicit managed filename. |
| Binary | `content_base64`, `filename`, optional `mime` | Explicit managed filename; base64 is validated strictly. |

Unknown fields and mixed source forms are rejected. The advertised schema
limits `path` to 4096 characters, `filename` to 200, and `mime` to 120. Local
configuration budgets and server upload validation also apply. In-memory text
is UTF-8 encoded; local file bytes are read by the adapter account. This is not
a sandbox limiting which local files an agent can request.

Example legacy-compatible call, on one JSON line:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"drop","arguments":{"zone":"default","items":[{"content":"Build succeeded\n","filename":"build.txt","mime":"text/plain"}],"replace":false}}}
```

Items are uploaded sequentially through the existing named HTTP-upload path
with `creation_method=filesystem_drop`. The adapter does not stage files
directly, register files, discover zones for the agent, or provide MCP tools
for reading/deleting/transferring storage. Use the [HTTP API](api.md) or
[CLI](cli.md) for the operations those interfaces implement.

## Results And Errors

A successful `tools/call` returns MCP `content` with a text block containing
JSON shaped as `{"zone":"default","items":[...],"errors":[]}`. Each successful
item is the HTTP upload payload, including the returned `reference`.
Per-item failures contain the zero-based `index` and a `message`. Some items
can succeed even when others fail; `isError` is true if `errors` is nonempty.
Do not blindly retry the whole call, particularly with `replace=true`.

Invalid tool arguments can instead produce an `isError: true` text result.
Protocol errors use JSON-RPC errors: parse error `-32700`, invalid request
`-32600`, invalid params `-32602`, unknown method `-32601`, and internal error
`-32603`. The stdin line budget is 64 MiB, separate from HTTP/content limits;
an oversized line is rejected and the adapter continues with the next line.
Blank lines are ignored. Notifications have no response. EOF ends the adapter.

## Protocol Negotiation

Legacy `initialize` accepts `2025-11-25`, `2025-06-18`, `2025-03-26`, and
`2024-11-05`. Other requested versions fall back to `2025-06-18` in that legacy
handshake. `notifications/initialized`, `notifications/cancelled`, and `ping`
are recognized; accepting cancellation notifications does not implement
asynchronous cancellation of an in-progress upload.

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"example","version":"1.0"}}}
```

For modern discovery:

```json
{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}
```

Modern requests carry `params._meta["io.modelcontextprotocol/protocolVersion"]`.
Unsupported values return `-32022` with `supported` and `requested` data.
Modern responses include `resultType: "complete"` and server-info metadata.
Discovery advertises a `3600000` ms TTL; the tool list advertises `300000` ms.
Both use `cacheScope: "public"`. These are adapter response fields, not a claim
that every MCP host supports that protocol revision.

For agent-host setup, see [integrations](../integrations.md) and
[agent output](../recipes/agent-output.md). Keep credentials out of committed
host configuration and keep stdout reserved for JSON-RPC messages.
