# Integrating Tools

A producer can keep generating ordinary files and use Pasteberth for artifact
history, supported previews and copy actions, comments, downloads, and selection.
That UI becomes available after explicit publication or registration, not from
watching every output directory. Choose the interface by locality and control,
not by whether the caller is a person, script, or agent.

## Choose an Interface

| Situation | Interface | Required locality | Daemon dependency |
|---|---|---|---|
| Paste, inspect, or retrieve interactively | Web UI | Browser can reach the service; no zone mount needed | Yes |
| Read a managed data file with an ordinary tool | Filesystem | Consumer can read the server-side zone path | No for the read itself |
| File is already at its final location | `register FILE` | Registering account can read the file and read, write, and traverse its parent | No; writes a sidecar locally |
| Publish files under daemon policy from a shared filesystem | `drop ZONE_DIRECTORY FILE...` | Sources readable locally; target path identifies a configured server zone | Always, including direct local staging |
| Publish from another machine | `drop --server URL --zone ID FILE...` | Sources readable on the CLI machine; no zone mount needed | Yes; HTTP upload |
| Implement browsing, retrieval, comments, deletion, or transfer in a client | HTTP API | Client can reach the service | Yes |
| Publish from an MCP host to a known zone | MCP `drop` | Paths are local to the adapter process, or content is supplied in the call | Yes; HTTP upload |
| Copy, move, rename, or delete managed pairs without HTTP | Filesystem CLI | Local configuration and access to the configured zone directories | No HTTP daemon call; uses local service/storage operations |

## Daemon-Backed Publication

Assume a running service, an existing zone, a readable nonempty `report.pdf`,
and an unused target filename. For a local target directory:

```sh
pasteberth drop --config /absolute/path/config.toml \
  /srv/workspaces/alpha/work/exchange ./report.pdf
```

The daemon resolves the canonical target directory against static zones and
eligible collection zones. This is not permission to write any arbitrary
directory. With a loopback endpoint and suitable filesystem access, `drop` can
stage bytes locally before asking the daemon to publish them; otherwise it
uses HTTP upload. The source file stays unchanged.

For a remote server, use a known zone ID rather than a path on the client:

```sh
pasteberth drop --server https://pasteberth.example.internal \
  --zone alpha-work-exchange ./report.pdf
```

Adapt the URL, including any deployment prefix. `drop` prompts after an
authentication challenge; `PASTEBERTH_PASSWORD` or `--password-stdin` supports
non-interactive use. Inject secrets through the calling environment rather than
checking them into scripts. Local CLI configuration can also impose client-side
limits. The daemon still validates publication and applies its zone policy.

Each successful source prints a reference. Multiple sources are independent,
and the command exits nonzero if any fail. Existing managed names require
explicit `--replace`; foreign names cannot be overwritten that way. See
[current result](recipes/current-result.md) and the [CLI reference](reference/cli.md).

## Registration Is Not Upload

`pasteberth register FILE` validates an existing regular file and creates or
refreshes its sidecar. It prints the resolved filesystem path, without applying
the zone's reference prefix. It does not rewrite, move, or overwrite the data.
It can run without the daemon and does not require the parent to be a configured
zone; only a directory exposed as a zone can make that item visible in the UI.

Registration reads the selected local configuration, or defaults, for content,
size, filename, image, and metadata validation. It does **not** consult the
running daemon's limits, enforce the zone's free-space reserve, run zone
retention, or verify the daemon's access to the pair. Later daemon publication
may evict registered items through retention. Use `drop` when publication must
go through daemon policy.

If a producer already created the file safely in its final directory, register
it after writing is complete. If it must first place a copy there, choose a
fresh name and use the guarded creation in
[register an existing file](recipes/register-file.md). Do not blindly `cp` over
an existing managed file and then refresh its sidecar: that bypasses managed
replacement and leaves a period of inconsistent data and metadata.

## MCP Hosts

The current adapter exposes exactly one application tool, `drop`. It requires
a known zone ID and 1 to 128 items. There is no MCP zone listing, item browsing,
download, comment, transfer, or filesystem-registration tool. Protocol
`server/discover` describes the MCP server, not available Pasteberth zones.
Obtain the zone ID through configuration, the operator, or the HTTP API.
A collection ID such as `@workspaces` is not a zone and cannot be a drop target.

For OpenCode or another local stdio MCP host, configure the host to launch this
executable and argument list, adapting both absolute paths:

```sh
/absolute/path/PasteBerth/pasteberth mcp --config /absolute/path/config.toml
```

This is a host-neutral invocation, not an OpenCode configuration schema. Use
the installed host's documented local MCP configuration syntax. If needed,
add `--server https://pasteberth.example.internal` to select the service URL.
Supply `PASTEBERTH_PASSWORD` in the adapter's environment when authentication
is enabled. The adapter never prompts on stdin because stdin/stdout carry
newline-delimited JSON-RPC.

Example arguments for the `drop` tool, with three alternative source forms:

```json
{
  "zone": "alpha-work-exchange",
  "items": [
    {"path": "/absolute/path/report.pdf"},
    {"filename": "summary.txt", "content": "Check totals against the source workbook.\n"},
    {"filename": "sample.bin", "content_base64": "AAFi"}
  ]
}
```

Each item uses exactly one of `path`, `content`, or `content_base64`.
`filename` is required for supplied content and forbidden with `path`; a path
uses its basename. `mime` is optional. Top-level `replace` is an optional
boolean, defaults to `false`, and applies to the call's uploads.

Paths belong to the machine, container, and account running the adapter, not
necessarily the service or the conversation's other tools. The adapter can read
any regular file its account can read; run it only under a trusted host with
appropriate filesystem access. It uploads over HTTP rather than directly
accessing zone storage.

Inspect the MCP tool result's `isError` and the JSON text containing `items` and
`errors`. A call can publish some items while others fail. Do not infer an
all-or-nothing bundle from one tool call. See the [MCP reference](reference/mcp.md)
and [agent-output recipe](recipes/agent-output.md).

## HTTP Clients and References

Use the [API reference](reference/api.md) for session login, same-origin headers,
multipart publication, overview reads, downloads, comments, archives, and
transfers. Preserve the session cookie and send a matching `Origin` or
`Referer` on unsafe requests; non-browser requests without either header are
also accepted under the documented API checks. There is no CORS interface for
arbitrary cross-origin browser applications.

**Unreleased, after 2.1.21:** use `/api/zones/{id}/items`, multipart `file`, and
the returned `content_url` for new HTTP integrations. Select `?schema=items` on
`GET /api/zones` and `POST /api/transfers`; their defaults remain legacy for
existing clients. Legacy routes, fields, and Python aliases remain supported
throughout 2.x, with removal no earlier than 3.0 and announced in advance.
See [compatibility](reference/api.md#compatibility-unreleased).

For retrieval without filesystem access, follow
[fetch a version-aware item](recipes/external-consumer.md). The standalone
example logs in, lists a known zone, conditionally downloads a selected file,
and verifies length and SHA-256 before replacing a local output. Unknown legacy
identity cannot pin the listed version. A stdout-report failure after local
publication is a distinct outcome, not rollback. This is not an SFTP deployer,
build service, or whole-zone snapshot API.

Zone responses and publication results carry server-side references. Per-zone
`reference_prefix`, `reference_suffix`, and list-format settings adapt those
strings to consuming tools. For example, backticks or `@` can delimit a path;
they are not shell syntax to execute blindly, URL generation, or access grants.
The consumer must see the referenced file at that path with suitable permissions.
Use authenticated download when it does not share that filesystem namespace.

Groups select views, not security boundaries. Do not use a group to isolate
clients or treat creation-method metadata as proof of producer identity. See
[deployment](deployment.md), [configuration](reference/configuration.md), and
[storage](reference/storage.md) for the actual trust and storage contracts.
