"""Trusted, fixed bridge rule; never callable from the model tool catalogue."""
import re
import subprocess
import sys


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {"add", "remove"} or not re.fullmatch(r"gtw[a-f0-9]{10}", sys.argv[2]):
        raise SystemExit(2)
    add, bridge = sys.argv[1] == "add", sys.argv[2]
    # --internal blocks routed egress. This additionally blocks INPUT to the VM
    # itself, including services on the Docker bridge gateway and IPv6 local IPs.
    for executable in ("iptables", "ip6tables"):
        rule = ["INPUT", "-i", bridge, "-m", "comment", "--comment", "voxquarry-only", "-j", "DROP"]
        check = subprocess.run([executable, "-w", "5", "-C", *rule], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        if add and check:
            subprocess.run([executable, "-w", "5", "-I", *rule], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif not add and not check:
            subprocess.run([executable, "-w", "5", "-D", *rule], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
