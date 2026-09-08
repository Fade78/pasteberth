# Concepts

Pasteberth stages artifacts in ordinary filesystem directories so they can be
used across working contexts. Deposit an artifact once, then retrieve it as a
file, a filesystem reference, or supported clipboard content.

This works for one person returning later, a developer moving between projects,
or several trusted people, scripts, and agents exchanging results. Web,
filesystem, CLI, API, and MCP access are choices of interface, not assigned
roles. The same participant can use several of them.

## Artifacts and Managed Items

An artifact can be a screenshot, text, HTML clipboard content, PDF, workbook,
CSV, archive, log, or another allowed file. Pasteberth stores and retrieves
files; it does not need to understand or edit their document formats. Images
and text have specialized preview and copy actions. Other files remain useful
as downloads or inputs to filesystem tools.

A managed item is a coherent data file and metadata sidecar:

```text
report.pdf
report.pdf.json
```

The sidecar describes the data file and can hold a short comment. Files without
coherent sidecars remain foreign: normal managed operations do not take them
over. Merely writing output into a zone does not put it in the Web UI. Publish
through the service or explicitly run `register` on an existing file.

The files remain ordinary files, not an opaque object store. Read them with
normal tools, but use managed operations to rename, move, or delete managed
items so the data and sidecar stay together. Directly overwriting a managed
data file can leave stale metadata. See [storage](reference/storage.md).

## Zones, Collections, and Groups

A **zone** is a named staging or exchange point backed by a real directory.
Choose zones by project, topic, or stage: `project-alpha`, `documents`,
`debugging`, or `review` are equally reasonable interpretations. A zone need
not have a sender, receiver, or place in a workflow.

A **zone collection** discovers existing directories that follow a configured
convention. For example, `/srv/workspaces/<project>/work/exchange` can give each
new project its own zone without a per-project configuration edit. Discovery
does not create the directory or register files inside it. It scans on service
reads, with background scans for Web overviews, rather than watching the
filesystem. See [provisioning](provisioning.md).

A **group** selects and presents zones. The same zone can appear in several
groups without duplicating files. An all-projects view and a focused view are
views of the same storage, not separate copies. Groups are not ACLs or
ownership boundaries.

## Publication and Retrieval

| Access path | What it does |
|---|---|
| Web UI | Paste or upload, inspect items, copy references or supported content, download, comment, select, delete, and transfer. |
| Filesystem | Read ordinary data files with tools that can access the zone directory. |
| `drop` CLI | Ask the daemon to publish one or more source files, leaving the sources unchanged. Local staging can avoid HTTP payload transfer, but still calls the daemon. |
| `register` CLI | Validate a file already in place and create or refresh only its sidecar, without contacting the daemon. |
| HTTP API | Publish and retrieve items, inspect zones, and perform documented managed operations. |
| MCP adapter | Publish paths or supplied content through the daemon to a known zone using the `drop` tool. It does not browse zones or retrieve items. |

A copied **reference** is formatted server-side filesystem text, such as
`@/srv/workspaces/alpha/work/exchange/report.pdf`. It is not a public URL.
The consuming process must see that path in the server's filesystem namespace
and have permission to read it. A browser on another machine usually needs a
download instead. Reference formatting can suit a tool's input syntax; it does
not mount storage or grant access. See [integrations](integrations.md).

## History, Comments, and Optional Workflows

Zones provide a history of currently managed items, not an unlimited archive.
Service publication applies the zone's retention policy and can remove older
managed pairs. Keep important results elsewhere or arrange backups. Registering
a file does not run zone retention, but a later service publication can include
that registered item in retention cleanup.

A short comment can carry useful context or an instruction: "Compare with the
previous screenshot" or "Use this CSV with the September inputs." It is
secondary metadata, not a task, assignment, approval, or conversation thread.

Copying or moving between zones can support an optional workflow. You choose
the meaning and direction, and may skip stages or move back. Pasteberth enforces
no order, participant roles, or approvals. Multiple selected files remain
separate items; there is no atomic workflow bundle or job queue.

## Boundaries

- Pasteberth is not a cloud drive, filesystem synchronization service, document editor, source-control system, or task manager.
- Service authentication uses a shared password, not individual user accounts or per-zone ACLs. Filesystem access depends on operating-system permissions.
- Publication and registration are explicit. Collection discovery exposes directories, not every file a tool happens to produce.
- Supported clipboard actions depend on content and browser capabilities. Arbitrary binary files do not become rich clipboard objects.
- A stable filename with explicit replacement is a current-result convention, not version history or guaranteed permanent storage.

Continue with [using Pasteberth](using-pasteberth.md),
[integrating tools](integrations.md), or the [documentation map](../GUIDE.md).
