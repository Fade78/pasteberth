# Pasteberth Documentation

Stage and retrieve files across browsers, filesystems, tools, and working
contexts. Choose a path below for the documented **2.1.24** runtime. Start with
the task you need, then follow its links for exact options and guarantees.
Changes marked **Unreleased** describe the working tree after 2.1.24, including
the generic item API and version-aware external-consumer recipe.

## Contents

| Path | Start Here | Continue With |
|---|---|---|
| Discover | [Project overview](README.md) | [Concepts](docs/concepts.md), [presentation and local demo](site/README.md) |
| Use | [Using Pasteberth](docs/using-pasteberth.md) | [Personal staging](docs/recipes/personal-staging.md), [documents](docs/recipes/documents.md), [selection bundles](docs/recipes/selection-bundle.md) |
| Multi-project | [Project zones](docs/recipes/project-zones.md) | [Provisioning](docs/provisioning.md), [collection contract](docs/zone-collection-contract.md) |
| Integrate | [Integrations](docs/integrations.md) | [CLI](docs/reference/cli.md), [HTTP API](docs/reference/api.md), [MCP](docs/reference/mcp.md), [external consumer (Unreleased)](docs/recipes/external-consumer.md), [register a file](docs/recipes/register-file.md), [script output](docs/recipes/script-output.md), [agent output](docs/recipes/agent-output.md), [current result](docs/recipes/current-result.md) |
| Deploy | [Deployment](docs/deployment.md) | [Configuration](docs/reference/configuration.md), [storage](docs/reference/storage.md), [operations](docs/operations.md), [troubleshooting](docs/troubleshooting.md) |
| Contribute | [Contributing](docs/contributing.md) | [Documentation maintenance](docs/documentation-maintenance.md), [release process](docs/release-process.md), [changelog](CHANGELOG.md), [license](LICENSE) |

Already have access to an instance? [Using Pasteberth](docs/using-pasteberth.md)
does not require you to install or administer it. Organize zones by project,
subject, or an [optional workflow](docs/recipes/zone-workflow.md); comments can
carry context without imposing stages, roles, or approvals.

The site has English and French presentation text. These Markdown guides and
the product UI are English. A static server may display Markdown source rather
than render it; GitHub renders the repository documentation.

## Existing Guide Links

The headings below retain the original guide anchors for existing bookmarks
and external links. Each points to the relocated material.

## 1. The Model

See [The Model](docs/reference/storage.md#the-model).

### What Pasteberth is for

See [What Pasteberth is for](docs/reference/storage.md#what-pasteberth-is-for).

### What Pasteberth is not

See [What Pasteberth is not](docs/reference/storage.md#what-pasteberth-is-not).

## 2. Requirements and Support

See [Requirements and Support](docs/deployment.md#requirements-and-support).

## 3. Installation

See [Installation](docs/deployment.md#installation).

### 3.1 Deployable copy

See [Deployable copy](docs/deployment.md#deployable-copy).

### 3.2 First start

See [First start](docs/deployment.md#first-start).

### 3.3 Configuration discovery

See [Configuration discovery](docs/reference/configuration.md#configuration-discovery).

## 4. Configuration

See [Configuration](docs/reference/configuration.md#configuration).

### 4.1 Top-level keys

See [Top-level keys](docs/reference/configuration.md#top-level-keys).

### 4.2 TLS

See [TLS](docs/reference/configuration.md#tls).

### 4.3 Authentication

See [Authentication](docs/reference/configuration.md#authentication).

### 4.4 Zones

See [Zones](docs/reference/configuration.md#zones).

### 4.5 Zone groups

See [Zone groups](docs/reference/configuration.md#zone-groups).

### 4.6 Zone collections

See [Zone collections](docs/reference/configuration.md#zone-collections).

## 5. Web UI

See [Web UI](docs/reference/storage.md#web-ui).

### 5.1 Paste and drop

See [Paste and drop](docs/reference/storage.md#paste-and-drop).

### 5.2 Content types

See [Content types](docs/reference/storage.md#content-types).

### 5.3 After a deposit: inspect and act

See [After a deposit: inspect and act](docs/reference/storage.md#after-a-deposit-inspect-and-act).

### 5.4 Retention

See [Retention](docs/reference/storage.md#retention).

## 6. Command-Line Interface

See [Command-Line Interface](docs/reference/cli.md#command-line-interface).

### 6.1 Configuration generation

See [Configuration generation](docs/reference/cli.md#configuration-generation).

### 6.2 Server

See [Server](docs/reference/cli.md#server).

### 6.3 Password

See [Password](docs/reference/cli.md#password).

### 6.4 Audit

See [Audit](docs/reference/cli.md#audit).

### 6.5 Filesystem drop

See [Filesystem drop](docs/reference/cli.md#filesystem-drop).

### 6.6 MCP stdio adapter

See [MCP stdio adapter](docs/reference/mcp.md#mcp-stdio-adapter).

### 6.7 Filesystem copy and move

See [Filesystem copy and move](docs/reference/cli.md#filesystem-copy-and-move).

### 6.8 Filesystem rename

See [Filesystem rename](docs/reference/cli.md#filesystem-rename).

### 6.9 Filesystem delete

See [Filesystem delete](docs/reference/cli.md#filesystem-delete).

### 6.10 Exit codes

See [Exit codes](docs/reference/cli.md#exit-codes).

## 7. Bash Completion

See [Bash Completion](docs/reference/cli.md#bash-completion).

## 8. Filesystem Layout and Data Ownership

See [Filesystem Layout and Data Ownership](docs/reference/storage.md#filesystem-layout-and-data-ownership).

## 9. Deployment

See [Deployment](docs/deployment.md#run-the-service).

### 9.1 Foreground operation

See [Foreground operation](docs/deployment.md#foreground-operation).

### 9.2 systemd user service

See [systemd user service](docs/deployment.md#systemd-user-service).

### 9.3 Caddy

See [Caddy](docs/deployment.md#caddy).

### 9.4 nginx

See [nginx](docs/deployment.md#nginx).

## 10. Security and Trust Boundaries

See [Security and Trust Boundaries](docs/deployment.md#security-and-trust-boundaries).

## 11. HTTP API

See [HTTP API](docs/reference/api.md#http-api).

### 11.1 Routes

See [Routes](docs/reference/api.md#routes).

### 11.2 Upload

See [Upload](docs/reference/api.md#upload).

### 11.3 Multiple operations

See [Multiple operations](docs/reference/api.md#multiple-operations).

### 11.4 Comments

See [Comments](docs/reference/api.md#comments).

### 11.5 Login clients

See [Login clients](docs/reference/api.md#login-clients).

## 12. Troubleshooting

See [Troubleshooting](docs/troubleshooting.md#troubleshooting).

### `ModuleNotFoundError: No module named 'PasteBerth'`

See [`ModuleNotFoundError: No module named 'PasteBerth'`](docs/troubleshooting.md#modulenotfounderror-no-module-named-pasteberth).

### `pasteberth` starts without authentication

See [`pasteberth` starts without authentication](docs/troubleshooting.md#pasteberth-starts-without-authentication).

### The server refuses to start with authentication enabled

See [The server refuses to start with authentication enabled](docs/troubleshooting.md#the-server-refuses-to-start-with-authentication-enabled).

### `audit` returns status 1

See [`audit` returns status 1](docs/troubleshooting.md#audit-returns-status-1).

### A file is not visible in the history

See [A file is not visible in the history](docs/troubleshooting.md#a-file-is-not-visible-in-the-history).

### A request returns `423 zone_busy`

See [A request returns `423 zone_busy`](docs/troubleshooting.md#a-request-returns-423-zone_busy).

### A filename replacement is refused

See [A filename replacement is refused](docs/troubleshooting.md#a-filename-replacement-is-refused).

### systemd cannot see a zone or temporary source

See [systemd cannot see a zone or temporary source](docs/troubleshooting.md#systemd-cannot-see-a-zone-or-temporary-source).

### The browser cannot use a returned path

See [The browser cannot use a returned path](docs/troubleshooting.md#the-browser-cannot-use-a-returned-path).

## 13. Tests and Development

See [Tests and Development](docs/contributing.md#tests-and-development).

## 14. Backup, Upgrade, and Recovery

See [Backup, Upgrade, and Recovery](docs/operations.md#backup-upgrade-and-recovery).

## 15. Current Limits and Roadmap

See [Current Limits and Roadmap](docs/deployment.md#current-limits-and-roadmap).

## 16. License

See [License](docs/contributing.md#license).
