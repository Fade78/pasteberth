# Pasteberth

<p align="center">
  <img src="docs/images/pasteberth-icon.svg" alt="Pasteberth icon" width="96">
</p>

**Stage, exchange, and pick up files across your working contexts.**

A screenshot to paste again. A spreadsheet to pass on. A report to retrieve.
Pasteberth gives files and clipboard content a place in ordinary directories,
with Web, filesystem, CLI, HTTP API, and MCP access for different operations.
Use it alone, with other people, or with scripts and agents. Nobody is assigned
to one interface.

[Documentation](GUIDE.md) · [Presentation and local demo](site/README.md) ·
[First local trial](#quick-start) · [Release history](CHANGELOG.md)

## Why Pasteberth?

Your work does not stay in one application. Pasteberth lets you deposit an
artifact in a named **zone**, then retrieve it in the form useful to the next
context:

- Copy a filesystem reference for a tool that can read that server-side path.
- Download the file in a browser, without needing SSH or a shared mount.
- Copy supported image, text, or HTML content back into the clipboard.
- Select several items to copy their references or download a ZIP when enabled.
- Read the ordinary data files directly with filesystem tools.

PDFs, workbooks, logs, archives, and other allowed files can be stored and
retrieved without Pasteberth interpreting their formats. Preview and clipboard
actions depend on the content and browser. Storage persists on disk, but zone
retention can remove older items: this is a working area, not a backup service.

**Unreleased:** downloads use already published zones without waiting for
discovery. Once files are acquired, even long ZIP transfers release zone locks
so managed writes can continue. Acquisition can still wait on storage or a writer;
see the [download contract](docs/reference/api.md#downloads-unreleased).
New external consumers can use the generic `items` API, stored SHA-256, and
`If-Match` to request a listed payload and verify its bytes before local
publication. Legacy routes remain supported throughout 2.x; see the
[external-consumer recipe](docs/recipes/external-consumer.md) and
[compatibility policy](docs/reference/api.md#compatibility-unreleased).

## Make It Yours

| Your situation | A useful starting point |
|---|---|
| Working alone across applications | [Paste now, copy again later](docs/recipes/personal-staging.md) |
| Exchanging documents or reviewing results | [Drop inputs and retrieve outputs](docs/recipes/documents.md) |
| Developing across many repositories or worktrees | [Give new projects their own zones](docs/recipes/project-zones.md) |
| Writing scripts or internal tools | [Publish outputs without building a retrieval UI](docs/recipes/script-output.md) |
| Working with agents | [Publish to a known zone through MCP](docs/recipes/agent-output.md) |
| Preparing environments and project templates | [Provision conventional exchange directories](docs/provisioning.md) |
| Collecting QA evidence or preparing a handoff | [Retrieve a selection as a bundle](docs/recipes/selection-bundle.md) |

Zones can represent projects, subjects, or stages of work. Copy or move managed
items between them, skip stages, or move back. A short comment can carry an
instruction or observation alongside the file. This is an
[optional, free-form workflow](docs/recipes/zone-workflow.md), not a required
process: Pasteberth has no assignments, approvals, or enforced transition order.
Dedicated task and review systems are better suited to those needs.

## Ordinary Files

A managed item is a data file with a coherent JSON metadata sidecar:

```text
report.pdf
report.pdf.json
```

Normal managed operations leave foreign files alone. Simply copying a file
into a zone does not add it to managed history; publish it or explicitly
register it. Use managed operations for renames, transfers, and deletion so
the data and sidecar stay together.

| Publication path | Use it when |
|---|---|
| Web paste, drop, or file picker | You want to deposit content from a browser. |
| `pasteberth drop` | You want the daemon to publish source files under its policies. It always contacts the daemon, even with direct local staging. |
| `pasteberth register FILE` | A completed file is already in place. It creates or refreshes only the sidecar, without contacting the daemon or rewriting data. |
| HTTP API | A client needs publication, authenticated retrieval, or other documented Web operations. |
| MCP `drop` | A trusted MCP host publishes local paths, UTF-8 content, or base64 content to a known zone. |

For example, with a running service, an existing zone, and an unused target
filename:

```sh
pasteberth drop --server https://pasteberth.example.internal \
  --zone alpha-work-exchange ./report.pdf
```

Adapt the URL and zone ID. The file is local to the CLI process; the zone is
on the server. Use `PASTEBERTH_PASSWORD` or `--password-stdin` for unattended
password authentication, or `PASTEBERTH_TOKEN` / `--token-stdin` for a scoped
bearer credential. Existing managed names require explicit `--replace`, and a
bearer grant must also allow named replacement.

Registration uses local validation, not the running daemon's retention or
per-zone free-space policy. See [which interface to use](docs/integrations.md),
[safe registration](docs/recipes/register-file.md), and
[publishing a current result](docs/recipes/current-result.md).

## Projects Without Repetition

Configure a collection once for a convention such as:

```text
/srv/workspaces/<project>/work/exchange
```

Your project or environment template creates each exchange directory with
suitable permissions. Pasteberth discovers eligible directories; no per-project
configuration edit or daemon restart is needed after the rule is loaded.
Groups can present all projects or focused views of the same zones, without
duplicating their files.

Discovery is request-triggered, with background scans for Web overviews. A
visible browser polls every 10 seconds and shows new zones after a scan
completes; this is not an instantaneous filesystem watcher. **Unreleased:**
overview polls reuse the completed registry during a cooldown of the greater
of 10 seconds or the last full refresh duration, measured from completion.
The next eligible poll can start one background job; mutations and explicit
legacy `/images` history reads still refresh or wait for an in-flight scan.
Downloads and generic `/items` listings neither start nor join scans, so new
zones stay unknown until published. Candidates must
meet the collection's path, ID, permission, and leaf-directory rules.
Discovery neither creates projects nor registers the files inside them.

Start with [project zones](docs/recipes/project-zones.md), or follow
[provisioning](docs/provisioning.md) when designing the environment convention.

## Quick Start

The documented runtime is **2.1.24**, officially supported on **Linux** with
**Python 3.11+** and a supported local filesystem. It has no third-party Python
runtime dependency.

Changes labelled **Unreleased** describe the working tree, not the `2.1.24`
release. They include generic item routes and conditional downloads, streamed
downloads, archive budgets, discovery
optimizations, and a date alongside the time for items older than 24 elapsed
hours in the selected-item panel; see the
[changelog](CHANGELOG.md#unreleased).

For an instance already installed, go straight to
[Using Pasteberth](docs/using-pasteberth.md). To explore without starting a
daemon, use the [presentation site's local demo](site/README.md). It uses a
memory adapter, not production authentication or storage.

For a first local runtime trial:

```sh
git clone --branch v2.1.24 --depth 1 https://github.com/Fade78/pasteberth.git
cd pasteberth
./PasteBerth/pasteberth
```

With **no existing configuration selected**, this starts an unauthenticated
loopback-only service at `http://127.0.0.1:8765/`. Its default zone is
`$XDG_DATA_HOME/pasteberth/storage/default`, normally
`~/.local/share/pasteberth/storage/default`. Do not expose this trial through a
proxy or a network listener. An existing configuration changes this behavior;
see [configuration discovery](docs/reference/configuration.md#configuration-discovery).

Paste a short note, select it, and try copying its content or downloading it.
Then follow [deployment](docs/deployment.md) to set up a configured service,
password, permissions, and HTTPS as appropriate. The deployable `PasteBerth/`
directory is independent of the documentation and development tooling.

## Boundaries And Support

- **Trusted participants:** one shared service password plus optional scoped bearer tokens; there are no individual Web accounts. Groups are views, not per-zone ACLs.
- **References are paths:** a copied reference is not a public URL. Its consumer must see the server-side path and have filesystem access.
- **Explicit publication:** MCP currently exposes only `drop`, not zone exploration or artifact retrieval.
- **Storage coordination:** managed operations coordinate cooperating Pasteberth processes, not arbitrary external editors or writers. Multi-file operations can partially succeed.
- **Retention, not archiving:** history covers currently managed files; a stable filename is not versioning or permanent retention.
- **Clipboard limits:** supported content can be copied; normal HTML copying is sanitized, but explicit raw-HTML copying and downloads preserve original content.

| Area | 2.1.24 support |
|---|---|
| Server | Linux, Python 3.11 or newer |
| Storage | Local filesystem with required backend capabilities |
| Browser tests | Chromium and Firefox; file-picker smoke coverage |
| Windows server | Backend tested under Wine; native Windows validation outstanding |
| macOS server | Backend present; native validation and official support outstanding |
| Network or exotic filesystems | Not guaranteed merely because they can be mounted |

Pasteberth is not a cloud drive, synchronization service, document editor, or
job queue. See [concepts](docs/concepts.md),
[security and deployment](docs/deployment.md#security-and-trust-boundaries),
and [storage guarantees](docs/reference/storage.md#transaction-scope).

## Documentation

[Choose a path in the guide](GUIDE.md): discover, use, work across projects,
integrate, deploy, or contribute. Exact settings and operations live in the
[configuration](docs/reference/configuration.md), [CLI](docs/reference/cli.md),
[HTTP API](docs/reference/api.md), and [MCP](docs/reference/mcp.md) references.
For day-to-day administration, use [operations](docs/operations.md) and
[troubleshooting](docs/troubleshooting.md).

The [site](site/README.md) is English by default with French selectable; the
technical documentation and product UI are English. Its historical screenshots
are labelled separately from its current frontend demo. See
[contributing](docs/contributing.md) to maintain the product or documentation.

## License

Pasteberth is licensed under the **GNU Affero General Public License v3.0 or
later**. See [LICENSE](LICENSE).
