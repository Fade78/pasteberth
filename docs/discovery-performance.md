# Discovery Performance

The optimizations below are **Unreleased** changes after 2.1.21. They reduce
duplicate work and background scan frequency; they do not impose a hard
filesystem deadline or make network filesystems officially supported.

## Identify the Slow Phase

A 13-second scan can be caused by remote metadata calls, sleeping storage,
large directory trees, symlink resolution, repeated traversal, or a combination.
Two elapsed-time samples alone cannot identify the cause.

Separate these costs before changing the discovery contract:

| Phase | What is measured or read |
|---|---|
| Collection discovery | Directory resolution, enumeration, eligibility, identities, and labels. `pasteberth audit` reports this duration, not the entire audit. |
| Registry installation | Opening/rechecking destinations, recovery for new destinations, filesystem identities, and group installation. |
| Zone overview | Per-zone histories and free-space information, in addition to requesting background discovery when eligible. |
| Browser | Request latency, response processing, and rendering. Normal visible-tab polling is every 10 seconds. |

For a controlled test instance, use the documented server logging option:

```sh
pasteberth serve --config /absolute/path/test-config.toml --log-level DEBUG
```

Do not start a second daemon on a live deployment's port or storage for this
measurement. Logs include each rule's `scan=...s` and new pass-cache observations
(`resolution`, `stat`, `enumeration`), followed by overall `scan=...s` and
`install=...s`. Cache counters are not kernel syscall counters: one strict path
resolution may perform several metadata reads. Timings are emitted after the
operation completes, so they do not guarantee a report from a hung call.

Compare the browser network timings of `/api/groups` and `/api/zones`, using the
correct authentication and deployment prefix. Groups use the last registry;
zones also read file histories and capacity. A fast groups response alongside
a slow zones response points to work outside directory discovery. The public
health endpoint does not exercise those storage reads.

Measure several runs on the actual office machine. Record rule roots, candidate
counts, cold/waking versus warm storage, and the phase timings. Avoid global
filesystem probes or recursive shell walks as a supposedly cheap health check:
they may touch the same unavailable mounts.

## Implemented Savings

- **Normalize before traversal:** strictly resolve directory aliases, then use directory identities to stop repeated traversal and cycles within each rule. Existing resolved-path matching and depth rules remain unchanged.
- **Share observations within one pass:** overlapping rules reuse resolution, stat, directory-entry inspection, and positive/negative candidate checks. Caches expire when discovery returns, so retargeted links and recovered directories are reconsidered on a later scan.
- **Keep path context:** enumeration caches use canonical paths, not just device/inode pairs. Bind aliases can expose different child mounts and must not borrow entry paths from another base.
- **Reject leaves early:** a matching candidate containing a subdirectory is rejected without inspecting the remaining entries' types. A partial leaf probe is not a complete traversal result; if deeper traversal is needed, it still happens.
- **Avoid retaining irrelevant files:** entry caches retain directories and errors, not successful regular-file records from every visited directory. The current directory is still materialized and sorted.
- **One refresh per explicit action:** operations that already checked the live zone registry do not immediately scan it again. Destination identity and operation-time storage checks remain in force.
- **Reuse device locks:** registry installation creates a device-space lock only when one does not already exist.

These are reductions in repeated work, not permission to ignore symlinks,
prune arbitrary regular expressions by lexical prefixes, or skip all mounted
filesystems. A directory link outside a collection base cannot produce an
eligible relative path, but resolving that link can itself require I/O.

## Poll Scheduling

Only one discovery refresh can be in flight. After it finishes, another
background refresh becomes eligible after:

```text
max(10 seconds, duration of the last complete refresh attempt)
```

The duration includes registry installation; the wait is measured from
completion, including startup, foreground refreshes, and failed attempts.
Background requests during this cooldown use the existing registry rather than
launch another scan. A later eligible request starts the next one; there is no
independent periodic watcher.

For example, a refresh taking 13 seconds is followed by at least 13 seconds
without a poll-triggered scan. The browser still polls, so actual starts depend
on request timing. This saves background work but can delay a new zone's
appearance. Explicit operations ignore the cooldown and either refresh or join
the in-flight scan. Continuous explicit traffic can therefore still cause
frequent scans.

The cooldown does **not** cache zone histories or free-space reads. An overview,
an explicit operation, startup, or shutdown waiting for an active scan can still
block on filesystem I/O. See the [collection contract](zone-collection-contract.md).

## Why Not a 100 ms Cutoff?

A Python deadline checked between calls cannot interrupt a blocked `stat`,
`scandir`, path resolution, directory open, or capacity query. A successful
100 ms probe does not prove that the next call will be fast either.

Waiting for a future with a timeout only stops the waiting caller. The worker
may remain inside the kernel. Submitting another worker every poll would pile
up stuck operations and open resources; executor shutdown can wait for those
operations as well. A device ID is not a reliable independent failure domain,
and obtaining one may already require a blocking filesystem call.

Waking storage on one pass and using it on the next is a useful goal, but it
requires scheduling and incomplete-result semantics, not merely a timeout
around today's scan function.

## Possible Next Steps

These strategies are **not implemented** by the current optimization:

| Strategy | Benefit | Required safeguards or tradeoff |
|---|---|---|
| Narrow roots and explicit excluded subtrees | Avoid irrelevant or known remote branches altogether | User-chosen configuration and documented path/link semantics; arbitrary regex matching alone cannot safely infer pruning. |
| Independent jobs per collection root | One slow root need not delay delivery of completed healthy roots | Fixed worker limit, at most one outstanding job per key, bounded backlog, fair retries, and generation checks. All workers can still become blocked. |
| Last-good registry and overview results with stale/busy state | Return promptly while a root or zone is unavailable | Distinguish incomplete scans from confirmed removal; never interpret a timed-out empty result as deletion. Preserve overlap/conflict rules. |
| Adaptive retry delay for slow roots | Avoid repeatedly waking or probing unavailable storage | Retry only after the previous job actually ends, and periodically test recovery. Avoid permanent exclusion after one slow sample. |
| Target-local explicit operations | Avoid scanning unrelated roots before acting on a known zone | Revalidate the target's identity and current eligibility without accepting stale ownership or missing overlapping-rule conflicts. |
| Incremental history/capacity refresh | Reduce work even when directory discovery is fast | Account for external writers, transfers, and retention; write admission must still check real current space and ownership. |

The most useful next measurement is whether time is spent in `scan`, `install`,
or the zone overview's storage reads. Choose the next layer from that evidence
rather than promising a universal 100 ms filesystem response.
