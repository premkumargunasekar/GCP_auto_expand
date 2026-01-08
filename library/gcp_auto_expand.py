#!/usr/bin/python3

from ansible.module_utils.basic import AnsibleModule
import json
import re
import ipaddress
import subprocess

UTIL_THRESHOLD = 70        # % threshold
BASE_PREFIX = 16           # Base pool prefix


def gcloud(cmd):
    full_cmd = f"gcloud --quiet {cmd}"
    result = subprocess.run(
        full_cmd, shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout else []


def count_used_ips(subnet_name, region, project):
    # VM NIC IPs
    vm_cmd = (
        f"compute instances list "
        f"--filter=\"networkInterfaces.subnetwork:({subnet_name})\" "
        f"--project {project} --format=json"
    )
    vms = gcloud(vm_cmd)

    vm_ips = []
    for vm in vms:
        for nic in vm.get("networkInterfaces", []):
            if subnet_name in nic.get("subnetwork", ""):
                vm_ips.append(nic.get("networkIP"))

    # Reserved internal IPs
    addr_cmd = (
        f"compute addresses list "
        f"--filter=\"subnetwork:({subnet_name})\" "
        f"--regions {region} "
        f"--project {project} --format=json"
    )
    addrs = gcloud(addr_cmd)

    return len(vm_ips) + len(addrs)


def run_module():
    module_args = dict(
        project=dict(type='str', required=True),
        dry_run=dict(type='bool', default=False)
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=False
    )

    project = module.params['project']
    dry_run = module.params['dry_run']

    try:
        subnets = gcloud(
            f"compute networks subnets list "
            f"--project {project} --format=json"
        )

        pattern = re.compile(r"^(.*)-(\d{3})-snt$")
        groups = {}

        for s in subnets:
            m = pattern.match(s.get("name", ""))
            if not m:
                continue
            groups.setdefault(m.group(1), []).append({
                "index": int(m.group(2)),
                "subnet": s
            })

        results = []
        changed_any = False

        for group_name in sorted(groups.keys()):
            chain = sorted(groups[group_name], key=lambda x: x["index"])
            latest = chain[-1]["subnet"]
            latest_index = chain[-1]["index"]

            subnet_name = latest["name"]
            region = latest["region"].split("/")[-1]
            network = latest["network"].split("/")[-1]

            details = gcloud(
                f"compute networks subnets describe {subnet_name} "
                f"--region {region} --project {project} --format=json"
            )

            cidr = ipaddress.ip_network(details["ipCidrRange"])
            total_ips = cidr.num_addresses
            used_ips = count_used_ips(subnet_name, region, project)
            utilization = round((used_ips / total_ips) * 100, 2)

            if utilization < UTIL_THRESHOLD:
                results.append({
                    "group": group_name,
                    "subnet": subnet_name,
                    "utilization": utilization,
                    "action": "skipped"
                })
                continue

            used_cidrs = [
                ipaddress.ip_network(s["ipCidrRange"])
                for s in subnets if s["network"] == latest["network"]
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
                    "group": group_name,
                    "subnet": subnet_name,
                    "utilization": utilization,
                    "action": "failed",
                    "reason": "No free CIDR"
                })
                continue

            new_name = f"{group_name}-{latest_index + 1:03d}-snt"

            if new_name in [s["name"] for s in subnets]:
                results.append({
                    "group": group_name,
                    "subnet": subnet_name,
                    "new_subnet": new_name,
                    "action": "already_exists"
                })
                continue

            if not dry_run:
                create_cmd = (
                    f"compute networks subnets create {new_name} "
                    f"--network {network} "
                    f"--range {candidate} "
                    f"--region {region} "
                    f"--project {project}"
                )
                subprocess.run(
                    f"gcloud --quiet {create_cmd}",
                    shell=True,
                    check=True
                )

            results.append({
                "group": group_name,
                "current_subnet": subnet_name,
                "new_subnet": new_name,
                "cidr": str(candidate),
                "region": region,
                "network": network,
                "utilization": utilization,
                "action": "created" if not dry_run else "dry-run"
            })

            changed_any = True

        module.exit_json(
            changed=changed_any,
            threshold=UTIL_THRESHOLD,
            dry_run=dry_run,
            results=results
        )

    except Exception as e:
        module.fail_json(msg=str(e))


def main():
    run_module()


if __name__ == '__main__':
    main()

