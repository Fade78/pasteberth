# Changelog

This file records user-visible changes to Pasteberth.

## [2.1.0] - 2026-09-04

### Dynamic Zones and Directory Storage

- discover existing repository directories through repeatable `[[autozone]]`
  rules without editing configuration or creating candidates;
- expose discovered zones through the service, HTTP API, CLI, groups, and Web UI;
- treat regular root files as managed directory-zone items with optional comment
  annotations, non-destructive `max_items` limits, and `incoming/` publication;
- report changed timestamps, blocked zones, capacity reasons, and `storage_limit`
  HTTP 507 errors.

## [2.0.1] - 2026-09-03

### Uploads and Retention

- warn and ask for confirmation before an upload would evict retained items;
- report retained filenames removed by a successful upload through the
  `retention_deleted` API field;
- keep retention enforcement authoritative on the server under the zone lock.

### Mobile Web UI

- make zone and action controls comfortable for touch input;
- keep the per-zone multiple-file picker usable on compact screens.

## [2.0.0] - 2026-09-02

### Deployment Layout

- make `PasteBerth/` the code-only deployment directory;
- expose `PasteBerth/pasteberth` as the sole executable entry point;
- resolve the deployment from an external symbolic link and support an explicit
  `PASTEBERTH_HOME` for a separately copied executable;
- move configuration and default storage to external XDG locations;
- remove the repository-root `bin/pasteberth` and compatibility `run.sh` paths.
- move upload, parser, image, authentication, and HTTP resource budgets into
  configuration, with explicit `"unlimited"` support where applicable;
- accept external symbolic-link paths after validating their resolved targets;
- add `pasteberth completion` for Bash.

This release changes the private Python module path to `PasteBerth.runtime`.
The public `pasteberth` command, HTTP API, zone data format, and existing
absolute zone paths remain unchanged.

## [1.8.1] - 2026-09-01

### Web UI and API

- allow line breaks in comments;
- keep the comment editor open for `Enter` and save with `Ctrl`+`Enter` (or
  `Command`+`Enter`).

## [1.8.0] - 2026-09-01

### Web UI and API

- add editable Unicode comments to managed items through the Web UI and the
  comment endpoint;
- add `show_full_path`, enabled by default for compatibility, to hide absolute
  filesystem references in the Web UI without changing API references;
- reject duplicate JSON object keys in comment requests instead of accepting
  ambiguous input.

### English Interface

- translate user-visible CLI, audit, configuration, HTTP, storage, filesystem,
  and browser messages to English;
- align operator documentation, generated configuration text, regression
  assertions, and platform error messages with the English interface.

## [1.7.0] - 2026-09-01

### CLI

- rename the filesystem subcommands to concise `drop`, `rename`, and `delete`
  names; the previous spellings are no longer accepted;
- update the command help, Bash completion, documentation, and regression
  coverage for the renamed interface.

## [1.6.5] - 2026-09-01

### Web UI and Synchronization

- poll visible browser tabs every 10 seconds for files published by `drop` or
  another client;
- mark newly discovered items and zones with a local `NEW` badge until the
  item is selected, without adding a backend push channel;
- add frontend contract and end-to-end coverage for the synchronization flow.

## [1.6.4] - 2026-09-01

### Bug Fixes and Compatibility

- avoid invoking Linux `renameat2(RENAME_NOREPLACE)` on shared filesystems
  exposed as 9p, DrvFS, virtiofs, or FUSE, using the guarded no-replace
  fallback instead;
- retry the guarded fallback when the underlying filesystem reports that
  `renameat2` is unavailable;
- add regression coverage for shared mounts and unsupported syscall errors.

## [1.6.3] - 2026-09-01

### Bug Fixes and Reliability

- allow bounded multipart framing and auxiliary fields around an upload while
  continuing to enforce `max_upload_size` on the extracted content;
- report an occupied listen port as a clean startup error instead of masking
  `EADDRINUSE` with an initialization `AttributeError`;
- add regression coverage for exact-limit uploads and occupied-port startup.

## [1.6.2] - 2026-09-01

### Documentation and CLI

- aligned the operator guide, README, configuration example, and Bash
  completion reference with the current CLI and platform support matrix;
- clarified the `serve --log-level` help text and documented CLI exit codes;
- documented the HTTP method and preview-capacity error responses.

## [1.6.1] - 2026-09-01

### Security and reliability

- bounded live authenticated sessions to 4096 by default, with configurable
  FIFO eviction;
- separated pending HTTP connections from active requests and hardened request
  timeout, logging, TLS configuration, and reverse-proxy path handling;
- bound storage operations to stable directory identities and reserved internal
  filename namespaces during recovery;
- sanitized HTML clipboard copies by default while retaining an explicit raw
  HTML action and preserving the stored source unchanged.

### Compatibility

- repository wrappers now use Python safe-path mode and do not inherit an
  ambient `PYTHONPATH`;
- mounted deployments can use a configured `url_prefix` without changing the
  browser's Origin semantics.

## [1.6.0] - 2026-08-31

### Added

- a semantic `platformfs` boundary with the existing Linux behavior preserved;
- a native Win32 filesystem backend for safe handles, identities, locking,
  transactions, recovery, and capability reporting;
- cross-platform configuration, audit, CLI, storage, and concurrency coverage.

### Security and compatibility

- the v1.5 sidecar and transaction-marker compatibility contract remains in
  place;
- foreign files, ownership checks, no-replace operations, and crash recovery
  remain guarded by platform capabilities;
- Windows behavior is covered under Wine, but native Windows/NTFS and macOS
  validation are not complete, so those platforms are not official targets yet.

## [1.5.0] - 2026-08-30

### Added

- project zones and group layouts for shared browser workspaces;
- image, text, and binary content handling with exact filesystem references;
- sequential multiple-file upload and multiple selection in the Web UI;
- copied reference lists, streamed ZIP downloads, and group deletion;
- `drop`, `rename`, and `delete` operations;
- configurable reference prefixes, suffixes, separators, and ZIP availability;
- a unified bidirectional clipboard-and-zones logo in the Web UI and
  documentation;
- password authentication, sessions, CSRF protection, proxy handling, and TLS
  termination options;
- transactional local storage with ownership sidecars, retention, crash
  reconciliation, and inter-process zone locking;
- Bash completion for the operator CLI;
- operator documentation and deployment examples.

### Security and compatibility

- foreign files and incoherent sidecars remain protected from managed
  replacement, rename, and deletion;
- generated configuration and password storage use private-file safeguards;
- image validation is bounded and structural, with upload and pixel limits;
- existing valid sidecars and transaction markers remain part of the v1
  compatibility contract.

The next major platform goal is native Windows and macOS support without
weakening these storage and security guarantees.
