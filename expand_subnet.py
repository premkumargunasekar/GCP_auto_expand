#!/usr/bin/env python3

import json
import re
import ipaddress
import subprocess
import sys

# ----------------------------
# INPUTS
# ----------------------------
if len(sys.argv) < 2:
    print(json.dumps({"error": "Usage: expand_subnet.py <gcp_project>"}))
    sys.exit(1)

PROJECT = sys.argv[1]
BASE_PREFIX = 16   # /16 base (can be changed)

# ----------------------------
# Helper to run gcloud
# ----------------------------
def gcloud(cmd):
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)

# ----------------------------
# 1. Get all subnets
# ----------------------------
try:
    subnets = gcloud(
        f"gcloud compute networks subnets list "
        f"--project {PROJECT} --format=json"
    )
except Exception as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(1)

# ----------------------------
# 2. Group subnets by prefix
#    Example:
#    ao-np-api-asia-south1-001-snt
#    -> group = ao-np-api-asia-south1
# ----------------------------
group_map = {}

pattern = re.compile(r"^(.*)-(\d{3})-snt$")

for s in subnets:
    name = s.get("name", "")
    m = pattern.match(name)
    if not m:
        continue

    group = m.group(1)
    index = int(m.group(2))

    group_map.setdefault(group, []).append({
        "index": index,
        "subnet": s
    })

if not group_map:
    print(json.dumps({"skip": True, "reason": "No expandable subnets found"}))
    sys.exit(0)

# ----------------------------
# 3. Pick ONE group to expand
#    (can be extended to loop all)
# ----------------------------
group_name = sorted(group_map.keys())[0]
group_entries = sorted(group_map[group_name], key=lambda x: x["index"])

last_entry = group_entries[-1]
last_subnet = last_entry["subnet"]
last_index = last_entry["index"]

# ----------------------------
# 4. Build next subnet name
# ----------------------------
next_index = f"{last_index + 1:03d}"
new_name = f"{group_name}-{next_index}-snt"

# ----------------------------
# 5. CIDR calculation
# ----------------------------
used_cidrs = [
    ipaddress.ip_network(s["ipCidrRange"])
    for s in subnets
]

last_net = ipaddress.ip_network(last_subnet["ipCidrRange"])

# derive base /16 (e.g., 10.10.0.0/16)
octets = last_net.network_address.exploded.split(".")
base_net = ipaddress.ip_network(
    f"{octets[0]}.{octets[1]}.0.0/{BASE_PREFIX}"
)

candidate = None
for sn in base_net.subnets(new_prefix=last_net.prefixlen):
    if sn not in used_cidrs:
        candidate = sn
        break

if not candidate:
    print(json.dumps({"error": "No free CIDR available"}))
    sys.exit(1)

# ----------------------------
# 6. Output for Ansible
# ----------------------------
output = {
    "group": group_name,
    "new_name": new_name,
    "cidr": str(candidate),
    "region": last_subnet["region"].split("/")[-1],
    "network": last_subnet["network"].split("/")[-1]
}

print(json.dumps(output))
