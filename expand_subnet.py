#!/usr/bin/env python3

import json
import re
import ipaddress
import subprocess
import sys

PROJECT = sys.argv[1]
GROUP_PREFIX = sys.argv[2]   # e.g. ao-np-api-asia-south1
BASE_PREFIX = 16             # /16 base

def gcloud(cmd):
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Exception(result.stderr)
    return json.loads(result.stdout)

# 1. Get all subnets
subnets = gcloud(
    f"gcloud compute networks subnets list "
    f"--project {PROJECT} --format=json"
)

# 2. Filter group subnets
pattern = re.compile(rf"^{GROUP_PREFIX}-(\d{{3}})-snt$")
group_subnets = [
    s for s in subnets if pattern.match(s["name"])
]

if not group_subnets:
    print(json.dumps({"skip": True, "reason": "No subnets found"}))
    sys.exit(0)

# 3. Sort & find last subnet
group_subnets.sort(key=lambda x: x["name"])
last = group_subnets[-1]

# 4. Next subnet name
last_num = int(pattern.match(last["name"]).group(1))
next_num = f"{last_num + 1:03d}"
new_name = f"{GROUP_PREFIX}-{next_num}-snt"

# 5. CIDR calculation
used_cidrs = [ipaddress.ip_network(s["ipCidrRange"]) for s in subnets]

last_net = ipaddress.ip_network(last["ipCidrRange"])
base_net = ipaddress.ip_network(
    f"{last_net.network_address.exploded.split('.')[0]}."
    f"{last_net.network_address.exploded.split('.')[1]}.0.0/{BASE_PREFIX}"
)

candidate = None
for sn in base_net.subnets(new_prefix=last_net.prefixlen):
    if sn not in used_cidrs:
        candidate = sn
        break

if not candidate:
    print(json.dumps({"error": "No free CIDR"}))
    sys.exit(1)

# 6. Output for Ansible
output = {
    "new_name": new_name,
    "cidr": str(candidate),
    "region": last["region"].split("/")[-1],
    "network": last["network"].split("/")[-1],
}

print(json.dumps(output))
