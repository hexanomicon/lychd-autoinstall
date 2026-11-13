# lychd-autoinstall

Wipe a VPS and rebuild it as openSUSE Tumbleweed (Btrfs + snapshots, SELinux enforcing) running
LychD, with one command. AutoYaST installs the OS; an Ansible playbook installs LychD on first boot.

## Rebuild a host

1. Get root SSH with the deploy key on *any* GRUB-booted Linux on the VPS (on AlphaVPS: panel →
   Rebuild OS → Debian 12, then `ssh-copy-id -i ~/.ssh/lychd_deploy.pub root@IP` with the panel's
   one-time password).
2. `python3 render.py hosts.json out` — writes `out/<host>.xml`.
3. `./reinstall.sh <host> <ip>` — stages the pinned Tumbleweed installer with the profile and
   reboots. AutoYaST wipes the disk; first boot installs LychD and runs the 21 acceptance checks
   (`/var/lib/lychd-firstboot/acceptance.json`, log in `/var/log/lychd-firstboot.log`).

## What gets pinned

| Input | Where |
| --- | --- |
| Tumbleweed snapshot | `hosts.json` → `tw_repo` (download.opensuse.org/history) |
| LychD source commit, Python, uv | `hosts.json` → `pins` |
| LychD image (exact ID + archive SHA-256) | `pins`, archive attached to this repo's releases |
| First-boot bundle | SHA-256 embedded in each profile |

## Access

- Admin user from `hosts.json` (SSH key only, passwordless sudo → `sudo -i` for root).
- Root has no password; for console emergencies use the provider's "reset root password"
  (works through `qemu-guest-agent`). Root SSH login is disabled.
- Firewall allows SSH only; the LychD Altar listens on localhost.

## Adding a host

Add an entry under `hosts` in `hosts.json` (admin user + public keys) and re-render.
Validate profiles against the AutoYaST schema (`yast2-schema-default` RNG) before using them.
