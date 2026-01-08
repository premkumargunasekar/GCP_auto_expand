#!/usr/bin/python3

from ansible.module_utils.basic import AnsibleModule
import json
import re
import ipaddress
import subprocess

UTIL_THRESHOLD = 10
BASE_PREFIX = 16


def gcloud(cmd):
    full_cmd = f"gcloud --quiet {cmd}"
    result = subprocess.run(
        full_cmd, shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    if result.stdout:
        return json.loads(result.stdout)
    return {}


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
        # 1. List all subnets
        subnets = gcloud(
            f"compute networks subnets list "
            f"--project {project} --format=json"
        )

        pattern = re.compile(r"^(.*)-(\d{3})-snt$")
        groups = {}

        for s in subnets:
            name = s.get("name", "")
            m = pattern.match(name)
            if not m:
                continue

            group = m.group(1)
            index = int(m.group(2))
            groups.setdefault(group, []).append({
                "index": index,
                "subnet": s
            })

        if not groups:
            module.exit_json(
                changed=False,
                results=[],
                reason="No expandable subnet chains found"
            )

        results = []
        changed_any = False

        # 2. Process ALL subnet chains
        for group_name in sorted(groups.keys()):
            chain = sorted(groups[group_name], key=lambda x: x["index"])
            latest = chain[-1]["subnet"]
            latest_index = chain[-1]["index"]

            region = latest["region"].split("/")[-1]
            subnet_name = latest["name"]
            network_name = latest["network"].split("/")[-1]

            # Describe subnet
            details = gcloud(
                f"compute networks subnets describe {subnet_name} "
                f"--region {region} --project {project} --format=json"
            )

            cidr = ipaddress.ip_network(details["ipCidrRange"])
            total_ips = cidr.num_addresses
            used_ips = int(details.get("usedIps", 0))
            utilization = round((used_ips / total_ips) * 100, 2)

            if utilization < UTIL_THRESHOLD:
                results.append({
                    "group": group_name,
                    "subnet": subnet_name,
                    "utilization": utilization,
                    "action": "skipped"
                })
                continue

            # Find used CIDRs in same VPC
            used_cidrs = [
                ipaddress.ip_network(s["ipCidrRange"])
                for s in subnets
                if s["network"] == latest["network"]
            ]

            octets = cidr.network_address.exploded.split(".")
            base_net = ipaddress.ip_network(
                f"{octets[0]}.{octets[1]}.0.0/{BASE_PREFIX}"
            )

            candidate = None
            for sn in base_net.subnets(new_prefix=cidr.prefixlen):
                if sn not in used_cidrs:
                    candidate = sn
                    break

            if not candidate:
                results.append({
                    "group": group_name,
                    "subnet": subnet_name,
                    "utilization": utilization,
                    "action": "failed",
                    "reason": "No free CIDR available"
                })
                continue

            new_index = f"{latest_index + 1:03d}"
            new_name = f"{group_name}-{new_index}-snt"

            # Idempotency check
            if new_name in [s["name"] for s in subnets]:
                results.append({
                    "group": group_name,
                    "subnet": subnet_name,
                    "new_subnet": new_name,
                    "action": "already_exists"
                })
                continue

            # 3. CREATE subnet (unless dry-run)
            if not dry_run:
                create_cmd = (
                    f"compute networks subnets create {new_name} "
                    f"--network {network_name} "
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
                "network": network_name,
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

