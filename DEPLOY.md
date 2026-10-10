# Deploying PACE

PACE runs on its own VM as the `pace` user under systemd, from a deploy-only
checkout in `/opt/pace`. It develops itself: a change PACE merges into
`nicholasyoder/pace` reaches the running server only when `deploy.sh` runs.

## Fresh VM

```sh
# copy scripts/ over first (the repo is private): scp -r scripts newvm:/tmp/pace/
sudo /tmp/pace/scripts/setup-vm.sh                 # packages, users, toolchains, sudoers, config
sudo -u pace-agent -i ~pace-agent/.local/bin/claude   # /login once, then /exit
sudo /tmp/pace/scripts/import-data.sh pace-data-….tar.gz   # only when moving from another VM
sudo /tmp/pace/scripts/deploy.sh                   # clone to /opt/pace, uv sync, start the service
```

Check `/etc/pace/config.toml` (bind = the Tailscale IP). Without imported data,
`deploy.sh` generates the PACE project's deploy key and stops: add it to the
repo with write access and rerun.

`run_windows` in the config uses the VM's local time. Check it with `timedatectl`
and set it with `sudo timedatectl set-timezone <Area/City>` (then restart `pace`).

## Moving from another VM

On the old VM: stop the server, then `scripts/export-data.sh /var/lib/pace` and
copy the archive (it holds private deploy keys) to the new VM. Canonical repos
re-clone themselves; agent worktrees and Claude sessions are recreated on demand
(in-progress plans resume as fresh sessions with the history replayed).

## Updating

```sh
sudo /opt/pace/scripts/deploy.sh          # waits until no runs are in progress
sudo /opt/pace/scripts/deploy.sh --force  # restart now; interrupted runs can be retried
journalctl -u pace -f                     # logs
```
