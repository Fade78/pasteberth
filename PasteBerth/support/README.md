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

For a shared POSIX zone, put the daemon account and every `register` writer in
the directory's group and use a `setgid` directory. The group must be present
in the running daemon process. After changing group membership, log out and in
again (or reboot) before restarting a `systemd --user` service; reloading the
unit alone does not refresh its supplementary groups. A successful `register`
can otherwise create a sidecar that the daemon cannot read.

An autozone rule can cover a whole project tree, for example
`/home/me/Depots/*/work/exchange`. When a new matching project directory is
created, the daemon discovers it during the next zone read and a visible browser
normally shows it within the next 10-second poll. No configuration edit or
service restart is needed; the directory must still be readable and satisfy the
rule's depth and subtree constraints.
