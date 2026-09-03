# PasteBerth Deployment Support

The executable for this deployment is `../pasteberth`. Link to that file from
the user's `PATH`; do not copy it away from this directory unless
`PASTEBERTH_HOME` is set to this deployment root.

```sh
ln -s /srv/PasteBerth/pasteberth ~/.local/bin/pasteberth
pasteberth --generate-config
pasteberth passwd
pasteberth audit
eval "$(pasteberth completion)"
```

The deployment is code only. Configuration, passwords, TLS keys, zones, and
runtime state belong outside this directory. Use `--config PATH` or
`PASTEBERTH_CONFIG` for configuration. The default configuration and storage
locations are under the XDG configuration and data directories.

`config.example.toml`, `deploy/pasteberth.service`, and
`completions/pasteberth.bash` are reference files. `pasteberth completion`
prints the completion script directly for shell evaluation.
