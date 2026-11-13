#!/usr/bin/env python3
"""Render per-host AutoYaST profiles and the first-boot bundle from hosts.json.

Usage: render.py hosts.json OUT_DIR
Writes OUT_DIR/<host>.xml with the first-boot bundle embedded and its SHA-256 pinned, so the
profile is the only file the installer needs.
"""
import base64
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent


def e(value):
    return escape(str(value))


def bundle_bytes(config):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, 0
            tar.addfile(info, io.BytesIO(data))

        add("firstboot.yml", (HERE / "bundle/firstboot.yml").read_bytes())
        add("files/app_probe.py", (HERE / "bundle/files/app_probe.py").read_bytes())
        add("group.json", json.dumps(config["pins"], indent=2).encode())
        for name, host in config["hosts"].items():
            host_vars = {
                "admin_user": host["admin_user"],
                "lychd_runtime_ssh_public_keys": config["runtime_keys"],
            }
            add(f"hosts/{name}.json", json.dumps(host_vars, indent=2).encode())
    return buf.getvalue()


def network_xml(name, host):
    # Explicit nameservers make YaST run netconfig, which fails under NetworkManager; DHCP supplies DNS.
    dns = "".join(f"<nameserver>{e(ns)}</nameserver>" for ns in host.get("dns", []))
    dns = f"<nameservers config:type=\"list\">{dns}</nameservers>" if dns else ""
    head = (
        "<networking>"
        "<backend>network_manager</backend>"
        f"<dns><hostname>{e(name)}</hostname>{dns}</dns>"
    )
    if "ip" not in host:
        return head + "<keep_install_network config:type=\"boolean\">true</keep_install_network></networking>"
    iface = host.get("iface", "eth0")
    return head + (
        "<keep_install_network config:type=\"boolean\">false</keep_install_network>"
        "<interfaces config:type=\"list\"><interface>"
        f"<name>{e(iface)}</name><bootproto>static</bootproto><startmode>auto</startmode>"
        f"<ipaddr>{e(host['ip'])}</ipaddr><prefixlen>{e(host['prefix'])}</prefixlen>"
        "</interface></interfaces>"
        "<routing><routes config:type=\"list\"><route>"
        f"<destination>default</destination><gateway>{e(host['gateway'])}</gateway><device>{e(iface)}</device>"
        "</route></routes></routing></networking>"
    )


def partitions_xml(host):
    disk = host.get("disk", "/dev/vda")
    if host.get("boot", "bios") == "uefi":
        boot = ("<partition><mount>/boot/efi</mount><filesystem config:type=\"symbol\">vfat</filesystem>"
                "<partition_id config:type=\"integer\">259</partition_id><size>512M</size></partition>")
    else:
        boot = "<partition><partition_id config:type=\"integer\">263</partition_id><size>8M</size></partition>"
    return (
        "<partitioning config:type=\"list\"><drive>"
        f"<device>{e(disk)}</device><disklabel>gpt</disklabel>"
        "<initialize config:type=\"boolean\">true</initialize><use>all</use>"
        "<enable_snapshots config:type=\"boolean\">true</enable_snapshots>"
        f"<partitions config:type=\"list\">{boot}"
        "<partition><mount>swap</mount><filesystem config:type=\"symbol\">swap</filesystem><size>2G</size></partition>"
        "<partition><mount>/</mount><filesystem config:type=\"symbol\">btrfs</filesystem><size>max</size>"
        "<subvolumes_prefix>@</subvolumes_prefix>"
        "</partition></partitions></drive></partitioning>"
    )


FIRSTBOOT_SH = r"""#!/bin/bash
set -euo pipefail
HOST='@HOST@'; BUNDLE_SHA='@SHA@'
exec >>/var/log/lychd-firstboot.log 2>&1
echo "=== lychd-firstboot $(date -Is) host=$HOST"
work=/var/lib/lychd-firstboot; chmod 0755 "$work"; cd "$work"
echo "$BUNDLE_SHA  bundle.tar.gz" | sha256sum -c -
rm -rf bundle && mkdir bundle && tar -xzf bundle.tar.gz -C bundle && chmod -R a+rX bundle
ansible-playbook -i localhost, -c local bundle/firstboot.yml -e @bundle/group.json -e @bundle/hosts/$HOST.json
touch done
echo "=== lychd-firstboot finished OK $(date -Is)"
"""

NM_DHCP = """install -d -m 0700 /etc/NetworkManager/system-connections
cat > /etc/NetworkManager/system-connections/wired-dhcp.nmconnection <<'LYCHD_EOF'
[connection]
id=wired-dhcp
type=ethernet
autoconnect=true

[ipv4]
method=auto

[ipv6]
method=auto
LYCHD_EOF
chmod 0600 /etc/NetworkManager/system-connections/wired-dhcp.nmconnection
"""

FIRSTBOOT_UNIT = """[Unit]
Description=LychD first-boot provisioning
Wants=network-online.target
After=network-online.target
ConditionPathExists=!/var/lib/lychd-firstboot/done

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lychd-firstboot
TimeoutStartSec=0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


def profile(name, host, config, sha, data):
    script = FIRSTBOOT_SH.replace("@HOST@", name).replace("@SHA@", sha)
    b64 = base64.encodebytes(data).decode()
    keys = "".join(f"<listentry>{e(k)}</listentry>" for k in host["admin_keys"])
    pkgs = "".join(f"<package>{e(p)}</package>" for p in config["packages"])
    chroot = (
        "set -e\n"
        "install -d -m 0755 /usr/local/sbin\n"
        "cat > /usr/local/sbin/lychd-firstboot <<'LYCHD_EOF'\n" + script + "LYCHD_EOF\n"
        "chmod 0700 /usr/local/sbin/lychd-firstboot\n"
        "install -d -m 0755 /var/lib/lychd-firstboot\n"
        "base64 -d > /var/lib/lychd-firstboot/bundle.tar.gz <<'LYCHD_EOF'\n" + b64 + "LYCHD_EOF\n"
        "cat > /etc/systemd/system/lychd-firstboot.service <<'LYCHD_EOF'\n" + FIRSTBOOT_UNIT + "LYCHD_EOF\n"
        "systemctl enable lychd-firstboot.service sshd.service NetworkManager.service NetworkManager-wait-online.service firewalld.service\n"
        # YaST's network handoff left the installed system offline; own the DHCP profile and SSH ingress.
        + (NM_DHCP if "ip" not in host else "") +
        f"echo {name} > /etc/hostname\n"
        "firewall-offline-cmd --zone=public --add-service=ssh\n"
    )
    return f"""<?xml version="1.0"?>
<!DOCTYPE profile>
<profile xmlns="http://www.suse.com/1.0/yast2ns" xmlns:config="http://www.suse.com/1.0/configns">
  <general><mode><confirm config:type="boolean">false</confirm><final_reboot config:type="boolean">true</final_reboot></mode></general>
  <language><language>en_US</language></language>
  <keyboard><keymap>english-us</keymap></keyboard>
  <timezone><hwclock>UTC</hwclock><timezone>Etc/UTC</timezone></timezone>
  <bootloader><loader_type>{'grub2-efi' if host.get('boot') == 'uefi' else 'grub2'}</loader_type></bootloader>
  {partitions_xml(host)}
  {network_xml(name, host)}
  <security><selinux_mode>enforcing</selinux_mode><lsm_select>selinux</lsm_select></security>
  <software>
    <products config:type="list"><product>openSUSE</product></products>
    <patterns config:type="list"><pattern>base</pattern><pattern>enhanced_base</pattern></patterns>
    <packages config:type="list">{pkgs}</packages>
  </software>
  <users config:type="list">
    <user><username>root</username><user_password>!</user_password><encrypted config:type="boolean">true</encrypted></user>
    <user>
      <username>{e(host['admin_user'])}</username><fullname>{e(host['admin_user'])}</fullname><uid>1000</uid>
      <user_password>!</user_password><encrypted config:type="boolean">true</encrypted>
      <authorized_keys config:type="list">{keys}</authorized_keys>
    </user>
  </users>
  <scripts><chroot-scripts config:type="list"><script>
    <filename>lychd-firstboot-setup.sh</filename><chrooted config:type="boolean">true</chrooted>
    <interpreter>shell</interpreter>
    <source><![CDATA[{chroot}]]></source>
  </script></chroot-scripts></scripts>
</profile>
"""


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    data = bundle_bytes(config)
    sha = hashlib.sha256(data).hexdigest()
    (out / "bundle.tar.gz").write_bytes(data)
    for name, host in config["hosts"].items():
        (out / f"{name}.xml").write_text(profile(name, host, config, sha, data))
    print(f"bundle sha256 {sha}; profiles: {', '.join(config['hosts'])}")


if __name__ == "__main__":
    main()
