#!/usr/bin/python3

from ansible.module_utils.basic import AnsibleModule
import json
import re
import ipaddress
import subprocess

UTIL_THRESHOLD = 80
BASE_PREFIX = 16


def ensure_adc():
    """
    Force gcloud to bind to Application Default Credentials (AWX)
    """
    subprocess.run(
        "gcloud auth application-default print-access-token --quiet",
        shell=True,
        check=True
    )


def gcloud(cmd):
    """
    Run gcloud command using ADC
    """
    full_cmd = f"gcloud --quiet {cmd}"
    result = subprocess.run(
        full_cmd, shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def run_module():
    module_args = dict(
        project=dict(type='str', required=True),
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=False
    )

    project = module.params['project']

    try:
        # 🔑 VERY IMPORTANT (AWX ADC BINDING)
        ensure_adc()

        # 1. List subnets
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
                skip=True,
                reason="No expandable subnet chains found"
            )

        # Pick ONE chain (deterministic)
        group_name = sorted(groups.keys())[0]
        chain = sorted(groups[group_name], key=lambda x: x["index"])
        latest = chain[-1]["subnet"]
        latest_index = chain[-1]["index"]

        region = latest["region"].split("/")[-1]
        subnet_name = latest["name"]

        # 2. Describe latest subnet
        details = gcloud(
            f"compute networks subnets describe {subnet_name} "
            f"--region {region} --project {project} --format=json"
        )

        cidr = ipaddress.ip_network(details["ipCidrRange"])
        total_ips = cidr.num_addresses
        used_ips = int(details.get("usedIps", 0))
        utilization = round((used_ips / total_ips) * 100, 2)

        if utilization < UTIL_THRESHOLD:
            module.exit_json(
                changed=False,
                skip=True,
                subnet=subnet_name,
                utilization=utilization,
                reason="Utilization below threshold"
            )

        # 3. Find next free CIDR
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
            module.fail_json(msg="No free CIDR available in base pool")

        new_index = f"{latest_index + 1:03d}"
        new_name = f"{group_name}-{new_index}-snt"

        module.exit_json(
            changed=True,
            action="expand",
            group=group_name,
            current_subnet=subnet_name,
            utilization=utilization,
            new_subnet=new_name,
            cidr=str(candidate),
            region=region,
            network=latest["network"].split("/")[-1]
        )

    except Exception as e:
        module.fail_json(msg=str(e))


def main():
    run_module()


if __name__ == '__main__':
    main()
