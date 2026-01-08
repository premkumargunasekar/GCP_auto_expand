#!/usr/bin/python3

from ansible.module_utils.basic import AnsibleModule
import subprocess
import json
import ipaddress
import re


BASE_PREFIX = 16   # Base CIDR pool (e.g. /16)


def gcloud(cmd):
    result = subprocess.run(
        f"gcloud --quiet {cmd}",
        shell=True,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout else []


def google_reserved_ips(total_ips):
    """
    Console-aligned approximation of Google internal IP reservations.
    These are NOT exposed via any API.
    """
    if total_ips <= 32:       # /29 – /27
        return 4
    elif total_ips <= 64:     # /26
        return 4
    elif total_ips <= 128:    # /25
        return 5
    elif total_ips <= 256:    # /24
        return 6
    elif total_ips <= 512:    # /23
        return 8
    elif total_ips <= 1024:   # /22
        return 10
    else:
        return int(total_ips * 0.03)  # safe upper bound


def count_api_used_ips(subnet_selflink, region, project):
    """
    Count API-visible IP usage only.
    """
    used = set()

    # VM NIC IPs
    vms = gcloud(
        f"compute instances list --project {project} --format=json"
    )

    for vm in vms:
        for nic in vm.get("networkInterfaces", []):
            if nic.get("subnetwork") == subnet_selflink:
                used.add(nic.get("networkIP"))

    # Reserved internal IPs
    addrs = gcloud(
        f"compute addresses list "
        f"--regions {region} "
        f"--project {project} "
        f"--format=json"
    )

    for addr in addrs:
        if addr.get("subnetwork") == subnet_selflink:
            used.add(addr.get("address"))

    # Gateway IP
    return len(used) + 1


def run_module():
    module = AnsibleModule(
        argument_spec=dict(
            project=dict(type="str", required=True),
            dry_run=dict(type="bool", default=False)
        ),
        supports_check_mode=False
    )

    project = module.params["project"]
    dry_run = module.params["dry_run"]

    try:
        subnets = gcloud(
            f"compute networks subnets list "
            f"--project {project} --format=json"
        )

        pattern = re.compile(r"^(.*)-(\d{3})-snt$")
        groups = {}

        for s in subnets:
            m = pattern.match(s["name"])
            if not m:
                continue
            groups.setdefault(m.group(1), []).append({
                "index": int(m.group(2)),
                "subnet": s
            })

        results = []
        changed = False

        for group in sorted(groups.keys()):
            chain = sorted(groups[group], key=lambda x: x["index"])
            latest = chain[-1]["subnet"]

            cidr = ipaddress.ip_network(latest["ipCidrRange"])
            total_ips = cidr.num_addresses

            api_used = count_api_used_ips(
                latest["selfLink"],
                latest["region"].split("/")[-1],
                project
            )

            google_reserved = google_reserved_ips(total_ips)

            used_ips = api_used + google_reserved
            free_ips = max(total_ips - used_ips, 0)

            # Free-IP reserve (IPAM-safe)
            reserve_ips = max(2, int(total_ips * 0.06))

            if free_ips > reserve_ips:
                results.append({
                    "group": group,
                    "subnet": latest["name"],
                    "total_ips": total_ips,
                    "api_used_ips": api_used,
                    "google_reserved_ips": google_reserved,
                    "used_ips": used_ips,
                    "free_ips": free_ips,
                    "reserve_ips": reserve_ips,
                    "action": "skipped"
                })
                continue

            # Find next free CIDR
            used_cidrs = [
                ipaddress.ip_network(s["ipCidrRange"])
                for s in subnets
                if s["network"] == latest["network"]
            ]

            octets = cidr.network_address.exploded.split(".")
            base_net = ipaddress.ip_network(
                f"{octets[0]}.{octets[1]}.0.0/{BASE_PREFIX}"
            )

            candidate = next(
                (sn for sn in base_net.subnets(new_prefix=cidr.prefixlen)
                 if sn not in used_cidrs),
                None
            )

            if not candidate:
                results.append({
                    "group": group,
                    "subnet": latest["name"],
                    "action": "failed",
                    "reason": "No free CIDR available"
                })
                continue

            new_name = f"{group}-{chain[-1]['index'] + 1:03d}-snt"

            if not dry_run:
                subprocess.run(
                    f"gcloud --quiet compute networks subnets create {new_name} "
                    f"--network {latest['network'].split('/')[-1]} "
                    f"--range {candidate} "
                    f"--region {latest['region'].split('/')[-1]} "
                    f"--project {project}",
                    shell=True,
                    check=True
                )

            results.append({
                "group": group,
                "current_subnet": latest["name"],
                "new_subnet": new_name,
                "cidr": str(candidate),
                "total_ips": total_ips,
                "api_used_ips": api_used,
                "google_reserved_ips": google_reserved,
                "used_ips": used_ips,
                "free_ips": free_ips,
                "reserve_ips": reserve_ips,
                "action": "created" if not dry_run else "dry-run"
            })

            changed = True

        module.exit_json(
            changed=changed,
            dry_run=dry_run,
            results=results
        )

    except Exception as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    run_module()
