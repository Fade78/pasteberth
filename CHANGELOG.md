# Changelog

This file records user-visible changes to Pasteberth.

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
- `filesystem-drop`, `filesystem-rename`, and `filesystem-delete` operations;
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
