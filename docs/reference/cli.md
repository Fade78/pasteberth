# CLI Reference

[Documentation map](../../GUIDE.md).

## Command-Line Interface

Use `pasteberth --help` for the parser's short reference and
`pasteberth --version` to print the version.

The command without a subcommand starts the server:

```text
pasteberth [--config PATH]
```

This form uses `log_level` from the selected configuration. To override it for
one invocation, use the explicit server subcommand:

```text
pasteberth serve [--config PATH] [--log-level LEVEL]
```

Global options are accepted before the subcommand. Subcommands also accept the
documented `--config` option after their name.

Syntax blocks use brackets for optional arguments; do not type the brackets.
`serve` starts the HTTP daemon; `drop` and `mcp` contact it. `register`,
`copy`, `move`, `rename`, and `delete` operate on the local
filesystem; `copy`, `move`, `rename`, and `delete` load their zone registry from
the CLI-selected configuration. They do not use the daemon's Web password.
See [configuration discovery](configuration.md#configuration-discovery).

### Configuration generation

```text
pasteberth [--config PATH] --generate-config [--force]
```

The command writes a secure generated configuration and refuses to overwrite
an existing target unless `--force` is present. Review the target before using
`--force`.

### Server

```text
pasteberth serve [--config PATH] [--log-level DEBUG|INFO|WARNING|ERROR]
```

The server runs in the foreground. Stop it with `Ctrl+C` when running
interactively. `--log-level` overrides the configuration for that invocation.

### Password

```text
pasteberth passwd [--config PATH]
```

The command prompts twice, requires at least eight characters, and writes a
salted scrypt hash to the configured password file. It never writes a plaintext
password or modifies a hash when the selected configuration is invalid.

### Audit

```text
pasteberth audit [--config PATH]
```

`audit` checks configuration, directories, ownership, permissions, listener
policy, host policy, and TLS settings without modifying the deployment.
It also prints an `INFO` line with the discovery duration, rule count, and
candidate count, including when no collection rules are configured. The duration
covers the filesystem scan and candidate construction, not the later per-zone
audit.

Exit codes are:

- `0`: no errors and no warnings;
- `1`: the configuration is usable but has warnings;
- `2`: one or more errors make the configuration unsafe or unusable.

Warnings include broad zone permissions, wildcard host checks, and other
conditions that may be intentional but deserve review.

### Filesystem drop

```text
pasteberth drop [--config PATH] [--server URL] [--insecure] [--password-stdin] \
  [--replace] ZONE_DIRECTORY SOURCE_FILE...
pasteberth drop [--config PATH] [--server URL] [--insecure] [--password-stdin] \
  [--replace] --zone ID SOURCE_FILE...
```

Without `--zone` and with multiple positional arguments, the first positional
argument is the target directory and the daemon resolves its canonical path
against static zones and eligible `[[zone_collection]]` candidates. This resolution is
performed by the daemon, so the client does not need a configuration file;
symlinked spellings of the configured and supplied paths resolve to the same
zone. With `--zone ID`, positional arguments are source files and the ID is sent
directly.

With no `--zone` and only one positional argument, `drop FILE` is rejected
because `drop` always contacts the daemon. Use `register FILE` for an existing
regular file; see [filesystem register](#filesystem-register).

For the target-directory form, `drop` first stages each source in a private
`.pbdrop-*.tmp` file when it is using a loopback daemon and the target is
writable. It then asks the daemon to validate the staged data and create the
managed data/sidecar pair. If direct staging is unavailable, it falls back to
the HTTP API. `--server URL` overrides the URL from configuration; with no
configuration, the default is
`http://127.0.0.1:8765`. One or more regular source files are accepted, remain
unchanged, and produce one returned reference per successful upload. A file
already present in the destination without a coherent sidecar remains foreign
for normal uploads. `register` creates or refreshes only the sidecar after
validating the regular file and its content; it never rewrites, moves, or
overwrites the existing data file.

With `--zone`, direct staging can also be attempted when the CLI configuration
identifies that zone's local directory. For a remote server, prefer `--zone ID`:
directory arguments are resolved on the client before being sent to the daemon
and are not portable workstation paths. Include the mount path in `--server`,
for example `https://pasteberth.example.internal/paste`.

The client also applies its local upload-size budget when reading source
files. A more permissive daemon configuration does not remove a stricter
budget in the CLI-selected configuration or built-in client defaults.

Direct staging still calls the daemon through the configured server URL. If the
loopback daemon uses a trusted self-signed HTTPS certificate, add `--insecure`;
this disables certificate verification only. Prefer a certificate whose SAN
contains the loopback address when possible.

Without `--replace`, an existing managed filename is refused. With
`--replace`, only a coherent Pasteberth-managed pair may be replaced. A foreign
file is never overwritten, even with `--replace`; `--replace` is not applicable
to `register`. Authentication prompts for a password after a `401`;
`PASTEBERTH_PASSWORD` and `--password-stdin` support non-interactive calls.

`--password-stdin` takes precedence over `PASTEBERTH_PASSWORD`. Do not put a real
password in a command history or a checked-in integration file. A protected
password file can supply stdin; trailing CR/LF characters are stripped:

```sh
pasteberth drop --server https://pasteberth.example.internal/paste \
  --zone project-alpha --password-stdin report.pdf < /secure/path/password.txt
```

Successful uploads print one formatted reference per source to stdout; errors
go to stderr. Sources are processed independently, so a nonzero exit can follow
successful deposits. Neither `drop` nor MCP accepts an arbitrary stdin content
stream: use a source file, or MCP's in-memory content fields.

### Filesystem register

```text
pasteberth register [--config PATH] FILE
```

`register` reads an existing regular file and creates or refreshes its JSON
sidecar without contacting the daemon. It never rewrites, moves, or replaces
the data file. A valid existing comment is retained; malformed or stale
metadata can be refreshed from the current data. Success prints the resolved
absolute path, not a zone-formatted reference.

The parent must be readable, traversable, and writable by the registering
account. The directory need not be a configured zone; registration there does
not make it discoverable. The file only appears in Pasteberth if its parent is
an active zone and the daemon can read the coherent pair.

Registration loads the CLI-selected configuration, or built-in defaults when
none is found. It checks upload size, accepted content kinds, filename, MIME,
image, and metadata budgets using that local configuration. It does not ask the
daemon to enforce its configuration, zone membership, retention, or per-zone
free-space reserve. `--config` does not change that into a server-backed
operation. Registration can therefore leave a zone above `retain` until a later
operation applies retention.

On POSIX, the sidecar uses the data file's group if the registering account
belongs to it. This does not apply the daemon's zone `file_group` or change the
data file's ownership. The daemon must also have the required group and
directory traversal permission. See [shared-zone checks](../troubleshooting.md#a-file-is-not-visible-in-the-history)
and the [register-file recipe](../recipes/register-file.md).

### Filesystem copy and move

```text
pasteberth copy [--config PATH] \
  /absolute/path/to/source/zone /absolute/path/to/target/zone report.pdf capture.png
pasteberth move [--config PATH] \
  /absolute/path/to/source/zone /absolute/path/to/target/zone report.pdf
```

The two directories must be the exact configured directories of different
zones. The filenames are basenames, not paths, and each name must identify a
coherent managed data/sidecar pair in the source zone. `copy` leaves the
source pair unchanged. `move` publishes the target pair before removing the
source pair. Existing target data, sidecars, foreign files, and malformed
sidecars are never replaced. A batch conflict is checked before any item is
copied. Successful operations print target references; a partial operation
prints an error and exits with code `1`.

The target's retention, free-space, and file-group settings from the CLI
configuration apply. A transfer is not an all-or-nothing batch: a later I/O or
retention failure may leave targets already published. Refresh both zones
before retrying. See [transaction scope](storage.md#transaction-scope).
Stored dates are preserved, but each publication protects its own filename
during retention. With `retain = 1`, even an older incoming item survives its
own retention pass and evicts the resident item. A later publication in the
batch can evict an earlier incoming item; that item is reported as a failure,
not a successful transfer, and a move keeps its source. A successful reference
is still not a promise of permanence against future operations. See
[retention](storage.md#retention) for ordering and protection details.

### Filesystem rename

```text
pasteberth rename [--config PATH] \
  /absolute/path/to/configured/zone old-name.pdf new-name.pdf
```

The source and target are basenames inside the configured zone. The data file
and its JSON sidecar use the managed rename transaction and recovery protocol.
This coordinates Pasteberth operations; it is not an atomic two-file rename for
uncoordinated external readers. An existing target is never replaced.

### Filesystem delete

```text
pasteberth delete [--config PATH] [--force] \
  /absolute/path/to/configured/zone report.pdf old-screen.png
```

Only coherent managed pairs are deleted. `--force` permits deletion when the
sidecar's recorded size is stale, but it does not make a foreign file or
malformed sidecar eligible for deletion.

### Exit codes

Unless a parser error prevents command dispatch, the CLI uses these codes:

| Code | Meaning |
|---:|---|
| `0` | The command completed successfully; a server also returns this after a normal stop. |
| `1` | An operational error occurred, a filesystem batch was only partly successful, a server could not bind, or `audit` found warnings. |
| `2` | CLI syntax, configuration, startup policy, or the requested deployment path is invalid or unusable. |

## Bash Completion

```text
pasteberth completion [--shell bash]
```

Bash is the only supported completion shell. `completion` does not take
`--config` after the subcommand.

The deployment provides a self-contained Bash completion script at
[`PasteBerth/support/completions/pasteberth.bash`](../../PasteBerth/support/completions/pasteberth.bash).
It completes commands, options, log levels, configuration paths, and ordinary
filesystem arguments for filesystem operations.

The command can emit the same script directly, which is convenient for a
temporary shell integration:

```sh
eval "$(pasteberth completion)"
```

Use it for the current shell:

```sh
source PasteBerth/support/completions/pasteberth.bash
```

Install it for the current user:

```sh
mkdir -p "$HOME/.local/share/bash-completion/completions"
install -m 0644 PasteBerth/support/completions/pasteberth.bash \
  "$HOME/.local/share/bash-completion/completions/pasteberth"
```

On systems using the global bash-completion directory, an administrator can
install it with:

```sh
sudo install -Dm0644 PasteBerth/support/completions/pasteberth.bash \
  /etc/bash_completion.d/pasteberth
```

After installation, start a new shell or source the file again. The completion
does not inspect configuration contents to enumerate zone IDs: filesystem
commands receive configured directory paths. Server-backed `drop` asks the
daemon to resolve its target; local filesystem commands validate against their
own selected configuration.

## MCP Adapter

See the [MCP reference](mcp.md) for `pasteberth mcp`, protocol versions, the
`drop` schema, authentication, and partial-result handling.
