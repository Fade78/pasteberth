# Publish a Current Result

**Need:** Repeatedly publish one named result such as `latest-report.pdf`,
rather than accumulate a separate filename for every run.

**Prerequisites:** A running daemon, an active zone, and a completed regular
source file at `./out/latest-report.pdf`, outside the managed destination.
The producer intentionally owns this naming convention. For the first
publication, neither the target data name nor its sidecar name is occupied.

## Steps

1. Generate the initial file outside the zone and publish it without replacement:

```sh
pasteberth drop --config /absolute/path/config.toml \
  /srv/workspaces/alpha/work/exchange ./out/latest-report.pdf
```

2. Check the successful result and its returned reference. Generate the next completed report at the same source basename, still outside the zone.
3. Deliberately replace the managed destination using the actual `drop` flag:

```sh
pasteberth drop --config /absolute/path/config.toml --replace \
  /srv/workspaces/alpha/work/exchange ./out/latest-report.pdf
```

4. Check the exit status, refresh the zone, and download the result to confirm the new content.

## Observable Result

Successful replacement updates the managed pair at the same zone path. The
basename comes from the source file; the CLI does not need a separate filename
option. Consumers using that reference can read the new result once published.
The source file remains unchanged by `drop`.

## Pitfalls

- Without `--replace`, an existing managed name is refused. Even with it, a foreign file or inconsistent pair is not an eligible replacement target.
- Do not overwrite the managed data file directly and then register it as a substitute for daemon-backed replacement.
- This is not versioning: replacement does not expose previous versions. Preserve historical reports separately when needed.
- A stable name is not pinned. Retention, deletion, zone removal, or a project rename can make the reference unavailable. A consumer already holding an open file may still read its earlier contents.
- One replacement does not make updates to several related results atomic. Coordinate consumers outside Pasteberth when they need a consistent multi-file release.

See [CLI reference](../reference/cli.md), [integrations](../integrations.md),
[storage](../reference/storage.md), and [script output](script-output.md).
