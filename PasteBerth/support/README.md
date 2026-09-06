# PasteBerth Deployment Support

The executable for this deployment is `../pasteberth`. Link to that file from
the user's `PATH`; do not copy it away from this directory unless
`PASTEBERTH_HOME` is set to this deployment root.

Do not start the private runtime with `python3 -m PasteBerth.runtime` from
inside this directory. That command cannot discover the package parent; use
the wrapper below instead.

```sh
ln -s /srv/PasteBerth/pasteberth ~/.local/bin/pasteberth
pasteberth --generate-config
pasteberth passwd
pasteberth audit
pasteberth drop --insecure /home/atelier/exchange report.md
pasteberth drop --insecure /home/atelier/exchange report.md screenshot.png
pasteberth register /home/atelier/exchange/existing.md
eval "$(pasteberth completion)"
```

The deployment is code only. Configuration, passwords, TLS keys, zones, and
runtime state belong outside this directory. Use `--config PATH` or
`PASTEBERTH_CONFIG` for configuration. The default configuration and storage
locations are under the XDG configuration and data directories.

For multi-file filesystem drops, the client can omit its configuration when it
shares the machine and target filesystem with the daemon. Without `--server`,
it tries `https://127.0.0.1:8765`; the daemon resolves the supplied target
against its configured static zones and `[[autozone]]` candidates. Use
`--insecure` for the trusted self-signed local certificate, or pass an explicit
`--server URL`. A one-file `drop FILE` is invalid because `drop` is always
server-backed. Use `register FILE` for a local-only sidecar operation; the
resulting sidecar must be readable by the daemon.

`config.example.toml`, `deploy/pasteberth.service`, and
`completions/pasteberth.bash` are reference files. `pasteberth completion`
prints the completion script directly for shell evaluation.
