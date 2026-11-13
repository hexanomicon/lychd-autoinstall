#!/bin/bash
# Wipe a VPS and reinstall it as Tumbleweed + LychD, unattended.
#   reinstall.sh NAME IP [SSH_USER]
# Needs root (or passwordless sudo) SSH on any GRUB-booted Linux on the target. It stages the
# pinned Tumbleweed installer kernel/initrd plus a small initrd holding out/NAME.xml, makes that
# the default boot entry and reboots. AutoYaST then wipes the disk; first boot installs LychD.
set -euo pipefail
name=$1; ip=$2; user=${3:-root}
here=$(cd "$(dirname "$0")" && pwd)
prof="$here/out/$name.xml"
[ -f "$prof" ] || { echo "missing $prof (run render.py first)"; exit 1; }
repo=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tw_repo"].rstrip("/"))' "$here/hosts.json")

sudo_=""; [ "$user" = root ] || sudo_="sudo -n"
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT

# Carry the provider's static IPv6 (the launchpad has it; Tumbleweed's DHCP does not).
v6=$(ssh "$user@$ip" "ip -6 -o addr show scope global | awk '{print \$4}' | head -1; ip -6 route show default | awk '{print \$3}' | head -1" | paste -sd' ')
echo "IPv6 from launchpad: ${v6:-none}"

# newc cpio holding /autoinst.xml, written with Python so the controller needs no cpio binary.
python3 - "$prof" "$work/autoinst.cpio.gz" "$v6" << 'PY'
import gzip, sys, time
data = open(sys.argv[1], encoding="utf-8").read()
v6 = sys.argv[3].split()
if len(v6) == 2:
    old = "[ipv6]\nmethod=auto\n"
    assert data.count(old) == 1, "profile has no auto IPv6 block to replace"
    data = data.replace(old, f"[ipv6]\nmethod=manual\naddress1={v6[0]},{v6[1]}\n")
data = data.encode()
def entry(name, mode, body):
    n = name.encode() + b"\0"
    hdr = "070701" + "".join(f"{v:08X}" for v in (0, mode, 0, 0, 1, int(time.time()), len(body), 0, 0, 0, 0, len(n), 0))
    out = hdr.encode() + n
    out += b"\0" * (-len(out) % 4) + body
    return out + b"\0" * (-len(out) % 4)
blob = entry("autoinst.xml", 0o100644, data) + entry("TRAILER!!!", 0, b"")
with gzip.open(sys.argv[2], "wb", 9) as f:
    f.write(blob)
PY

scp -q "$work/autoinst.cpio.gz" "$user@$ip:/tmp/lychd-autoinst.cpio.gz"
ssh "$user@$ip" "$sudo_ bash -s -- '$repo'" << 'REMOTE'
set -euo pipefail
repo=$1
fetch() { if command -v curl >/dev/null; then curl -fsSL --retry 3 -o "$1" "$2"; else wget -q -O "$1" "$2"; fi; }
install -m 0644 /tmp/lychd-autoinst.cpio.gz /boot/lychd-autoinst.cpio.gz
fetch /boot/tw-linux  "$repo/boot/x86_64/loader/linux"
fetch /boot/tw-initrd "$repo/boot/x86_64/loader/initrd"
# Cross-check the downloads against the repo's CHECKSUMS file.
if fetch /tmp/tw-CHECKSUMS "$repo/CHECKSUMS"; then
  for f in linux initrd; do
    want=$(awk -v p="boot/x86_64/loader/$f" '$2==p{print $1}' /tmp/tw-CHECKSUMS)
    have=$(sha256sum "/boot/tw-$f" | cut -d' ' -f1)
    [ -z "$want" ] || [ "$want" = "$have" ] || { echo "checksum mismatch for $f"; exit 1; }
  done
fi
if mountpoint -q /boot; then p=""; else p=/boot; fi
cat > /etc/grub.d/09_lychd_autoinstall << EOF
#!/bin/sh
cat << 'GRUB'
menuentry 'lychd-tw-autoinstall' --id lychd-tw-autoinstall {
  insmod gzio
  insmod part_gpt
  insmod part_msdos
  insmod ext2
  insmod btrfs
  insmod xfs
  search --no-floppy --file --set=root $p/tw-linux
  linux $p/tw-linux autoyast=file:///autoinst.xml install=$repo/ ifcfg=*=dhcp self_update=0 textmode=1
  initrd $p/tw-initrd $p/lychd-autoinst.cpio.gz
}
GRUB
EOF
chmod 0755 /etc/grub.d/09_lychd_autoinstall
sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=lychd-tw-autoinstall/; s/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=3/' /etc/default/grub
grep -q '^GRUB_DEFAULT=lychd-tw-autoinstall' /etc/default/grub || echo 'GRUB_DEFAULT=lychd-tw-autoinstall' >> /etc/default/grub
if command -v update-grub >/dev/null; then update-grub; else grub2-mkconfig -o /boot/grub2/grub.cfg; fi
grep -q lychd-tw-autoinstall /boot/grub*/grub.cfg
echo "staged ($repo); rebooting into the Tumbleweed installer"
(sleep 2; systemctl reboot) >/dev/null 2>&1 &
REMOTE
echo "$name ($ip): installer started. Watch it in noVNC; LychD comes up on first boot (~25-40 min)."
