# Pasteberth Autozone Contract

Status: implemented contract. This document describes the `[[autozone]]`
feature and the sidecar-free directory storage used by discovered zones.

## 1. Purpose

An autozone rule discovers existing directories and exposes them as Pasteberth
zones without writing those directories into `config.toml`. A discovered zone
is dynamic configuration: it exists while its directory satisfies the rule and
disappears when it no longer does.

The primary use case is a repository tree such as:

```text
/home/me/Depots/
  project-a/work/exchange/
  project-b/work/exchange/
  project-c/work/exchange/
```

The operator configures one rule for `^[^/]+/work/exchange$`, rather than one
static `[[zones]]` entry per repository.

Autozone discovery is read-only with respect to the directory tree and the
configuration file. It does not create candidates, rewrite the configuration,
or migrate an existing static zone.

## 2. Configuration

The spelling of the configuration table is `[[autozone]]` (singular table
name, repeatable). A rule has the following shape:

```toml
[[autozone]]
base_directory = "/home/me/Depots"
pattern = "^[^/]+/work/exchange$"
max_depth = 4
group = "Repositories"
label_mode = "git-or-relative"

# Zone template.
storage_mode = "directory"
max_items = 1000
min_free_percent = 2.0
reference_prefix = "@"
reference_suffix = ""
reference_list_prefix = ""
reference_list_suffix = ""
reference_separator = ","
allow_zip_download = true
color = "#304237"

# Options used only if [[groups]] does not define this group.
group_layout = "tab"
group_hide_empty = false
group_show_count = true
```

### 2.1 Rule keys

| Key | Required/default | Meaning |
|---|---|---|
| `base_directory` | required | Absolute directory below which candidates are searched. It is resolved by the server, not by the browser. It is never created by discovery. |
| `pattern` | required | Case-sensitive Python regular expression, applied with `fullmatch` to the normalized resolved relative path. The path separator supplied to the expression is `/`. |
| `max_depth` | `4` | Maximum number of path components in the candidate path relative to `base_directory`, after path resolution. It is a traversal bound, not a filesystem quota. |
| `group` | required | Name of the group to which every candidate from this rule is added. |
| `label_mode` | `git-or-relative` | `git-or-relative` uses the nearest Git worktree directory name when found; `relative` always uses the normalized relative path. |
| `storage_mode` | `directory` | Storage contract applied to every discovered zone. `directory` is the sidecar-free mode defined in section 6. The existing `sidecar` mode may be supported by a later implementation, but is not implied by this contract. |
| `max_items` | required for `directory` | Positive maximum number of regular files at the zone root. Exceeding it blocks Pasteberth writes; it never causes automatic deletion. |
| `min_free_percent` | `2.0` | Minimum free-space reserve on the filesystem. It uses the same meaning as the static-zone setting. |
| `reference_prefix` | `@` | Prefix for one returned filesystem reference. |
| `reference_suffix` | empty | Suffix for one returned filesystem reference. |
| `reference_list_prefix` | empty | Prefix for a copied reference list. |
| `reference_list_suffix` | empty | Suffix for a copied reference list. |
| `reference_separator` | `,` | Separator for a copied reference list. |
| `allow_zip_download` | `true` | Whether multiple selected files can be downloaded as a ZIP. |
| `color` | `#243447` | Zone color, subject to the existing contrast validation. |
| `group_layout` | `area` | Layout for a generated group only. |
| `group_hide_empty` | `false` | Empty-group visibility for a generated group only. |
| `group_show_count` | `true` | Zone-count visibility for a generated group only. |

`retain` is not used by `storage_mode = "directory"`. A directory zone uses
`max_items` as a blocking limit and never evicts files automatically. Supplying
both is an audit warning and does not restore destructive retention semantics.

The ordinary global and zone validation rules still apply: NUL characters,
relative paths, invalid colors, invalid percentages, invalid regular
expressions, and unsafe identifiers are configuration errors.

### 2.2 Configuration with no static zones

A configuration is valid when it has at least one `[[autozone]]` rule, even if
no directory currently matches. It starts with zero zones and can later expose
dynamic zones. A configuration with neither `[[zones]]` nor `[[autozone]]` is
invalid.

Static `[[zones]]` entries remain supported. They and autozones are combined in
one zone registry.

## 3. Candidate discovery

Each rule is evaluated independently.

1. Pasteberth resolves `base_directory` and verifies that it is an existing
   directory. Discovery does not create it.
2. The scanner walks existing directories below that base, following directory
   symlinks and other directory aliases. Symlinks are not rejected merely
   because they are symlinks.
3. A resolved directory is visited at most once during one scan. Directory
   identity and resolved paths are used to stop cycles and to deduplicate
   aliases.
4. The candidate path is resolved before matching. For a candidate under the
   resolved base, the scanner computes a normalized relative path with `/`
   separators and applies `pattern` with `fullmatch`.
5. `max_depth` counts components of that relative resolved path. For example,
   `project-a/work/exchange` has depth 3, regardless of the absolute length of
   `base_directory`.
6. A candidate must be an existing, accessible directory. Discovery never
   creates a missing candidate.
7. A candidate is accepted only when its subtree contains no user-created
   subdirectory other than the reserved Pasteberth directories for its storage
   mode. Regular files are allowed at the zone root.

A resolved target that cannot be represented as a relative path below the
resolved `base_directory` is not a match for this relative-path rule. This is
not a prohibition on following links; it is the consequence of defining the
pattern and depth relative to the rule base.

The scan must not follow file symlinks as content. Directory links are followed
for discovery; content entries are still subject to the existing regular-file
and no-follow safety rules.

### 3.1 Reserved directories

For `storage_mode = "directory"`, the only directories allowed below a
candidate are:

```text
incoming/
.pasteberth/
```

The contents of those directories are Pasteberth internal state and are not
zone content. A user directory beside them, or below them where the storage
contract does not permit it, makes the candidate ineligible. The scanner keeps
the candidate out of the public zone list and reports the reason through
`audit` or a diagnostic log.

## 4. Zone identity and labels

### 4.1 Stable identifier

The public zone ID is derived only from the normalized relative path, never
from the set of currently discovered candidates and never from a Git remote.

The first implementation uses this deterministic transformation:

1. normalize path components and join them with `-`;
2. convert the result to lowercase;
3. validate it with the existing zone-ID rule
   `^[a-z0-9][a-z0-9_-]{0,63}$`.

For example:

```text
relative path: project-a/work/exchange
zone ID:      project-a-work-exchange
```

There is no automatic suffixing and no truncation. A candidate is ignored when
its generated ID is invalid, longer than 64 characters, or collides with a
static zone or another autozone. `pasteberth audit` must report the candidate,
the generated ID, and the reason.

If multiple lexical paths resolve to the same directory, they represent one
candidate. The lexicographically smallest matching relative path is the
canonical path used for its ID and label.

### 4.2 Label

With `label_mode = "git-or-relative"`, Pasteberth walks upward from the
resolved candidate and looks for the nearest ancestor containing `.git` as a
directory or a regular Git worktree file. The basename of that worktree root
is the label. Pasteberth does not execute Git and does not inspect remotes.

If no Git worktree is found, the normalized relative path is the label.

With `label_mode = "relative"`, the normalized relative path is always the
label. Labels may be duplicated; IDs may not.

## 5. Group membership

Every accepted candidate is explicitly added to the group named by `group`.
This membership is independent of whether the candidate's generated ID happens
to match a regex in an existing group.

If an ordinary `[[groups]]` entry with the same name exists, that ordinary
entry is authoritative for `layout`, `hide_empty`, `show_count`, and its other
group options. The autozone's `group_layout`, `group_hide_empty`, and
`group_show_count` values are ignored for effective behavior. The candidate
membership is still added to that group.

If no ordinary group has the name, Pasteberth creates a generated group for
the lifetime of the rule. The generated group has an explicit autozone
membership and uses the three `group_*` options from the first rule in TOML
order that names it. Its API selection is reported as `autozone`, not as a
regex selection.

When several autozone rules name the same generated group and provide different
group options, the first rule remains effective and `pasteberth audit` reports
the conflict. When an ordinary group is later added, its options take priority
over all generated defaults.

Generated groups remain defined while their rule exists, even when they have
zero candidates. `hide_empty` controls whether an empty generated group is
shown in the browser. Removing the rule from configuration removes the
generated group after the next service start; discovery never edits the
configuration.

Autozone membership is also included when resolving `other`: the existing
group semantics determine the ordinary members, and the explicit autozone
membership is added rather than silently discarded.

## 6. Sidecar-free directory storage

An autozone using `storage_mode = "directory"` has this layout:

```text
zone/
  report.pdf                         # managed because it is at the root
  incoming/
    pbinc_report.pdf                 # incomplete construction file
  .pasteberth/
    meta/
      pbmeta_report.pdf.json         # optional comment annotation
```

The presence of a regular file at the zone root is the sole authority that the
file is managed. No metadata file is required for visibility, ownership, URL
generation, preview, deletion, or download.

`pbinc_<name>` is a data filename, not a metadata JSON file. It is reserved in
`incoming/` to signal that a producer is still constructing the file. The
producer publishes a completed cross-filesystem copy by renaming it from
`pbinc_<name>` to `<name>` inside `incoming/`, after which Pasteberth can move
`<name>` atomically to the zone root. When the source and destination are on the
same filesystem, the producer may move directly into the zone root; the atomic
`mv` is the publication event.

A normal external `mv` may replace an existing root filename. This cannot be
confirmed in advance by Pasteberth, so the replacement is accepted. The next
poll identifies the changed item and marks it `NEW`; an old annotation is not
applicable when its file fingerprint no longer matches the content.

`pbmeta_<name>.json` is an optional comment file. Its absence means no
comment. It contains only the comment, a format version, and a portable file
fingerprint sufficient to reject an annotation after an external replacement.
An invalid, missing, stale, or orphaned annotation never hides or deletes the
root data file.

The root reader ignores `incoming/`, `.pasteberth/`, reserved internal names,
links, and non-regular entries. A regular root file with no annotation is still
a complete managed item.

## 7. Limits and blocked zones

For a directory zone, `max_items` counts regular managed files at the root;
internal files and `pbinc_*` files do not count. The existing
`min_free_percent` reserve also applies.

When either limit is exceeded:

- the zone is reported as `blocked` in the overview;
- the Web UI displays a warning triangle;
- Pasteberth refuses new Web uploads and controlled publications;
- reads, previews, archives, and explicit deletions remain available where
  the filesystem permits them;
- no file is deleted automatically.

An external process can still bypass Pasteberth and move a file into the root.
The next poll counts it and keeps the zone blocked. The blocking state is an
application response to observed state; it is not a replacement for filesystem
quotas.

## 8. Refresh and lifecycle

The existing browser synchronization remains polling-based. No directory
watcher, SSE endpoint, or batch worker is required by this contract.

On each normal zone-overview refresh, Pasteberth reevaluates autozone rules at
most once for that refresh cycle. The current browser interval is 10 seconds,
so a newly created candidate normally appears within one visible polling
interval. A removed or invalid candidate disappears on the same schedule.

The service maintains an in-memory dynamic registry containing the current
zone configuration, destination, locks, and group memberships. The registry is
replaced as a coherent snapshot. An operation that already holds a zone lock
continues using its snapshot; a later request resolves the current registry.

Dynamic zones use the same API shape as static zones:

- `GET /api/zones` includes discovered zones and their current group IDs;
- `GET /api/zones/{id}/images` reads the current candidate directory;
- upload, comment, delete, preview, archive, and CLI directory resolution use
  the current dynamic registry;
- a zone that disappeared between two requests returns the normal unknown-zone
  or destination error rather than accessing an unrelated directory.

## 9. Audit and diagnostics

`pasteberth audit` must validate rules without creating directories or changing
files. It reports:

- missing, unreadable, or invalid base directories;
- invalid regular expressions and depth values;
- candidates rejected for user subdirectories;
- resolved-path aliases and duplicate candidates;
- invalid, overlong, or colliding generated IDs;
- static-zone precedence over an autozone candidate;
- conflicting generated-group options;
- use of `retain` with directory storage;
- the number of currently discovered candidates per rule.

An individual bad candidate must not hide otherwise valid candidates from the
same rule. A malformed rule or unsafe base configuration is a configuration
error; a transient candidate failure is a diagnostic and leaves that candidate
out of the registry until a later poll succeeds.

## 10. Non-goals

This contract does not add migration of existing sidecar zones. A static zone
continues using its current sidecar contract until an explicit future migration
feature is designed.

It does not make arbitrary nested project trees into recursive Pasteberth zones:
an accepted candidate is itself a zone and may contain only its reserved
Pasteberth directories. It does not provide cross-server synchronization,
filesystem quotas, or a persistent database of discovered zones.
