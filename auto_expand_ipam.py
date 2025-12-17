#!/usr/bin/env python3

import ipaddress
from googleapiclient import discovery
from google.oauth2 import service_account

PROJECT = "network-automation-471112"
THRESHOLD = 80

creds = service_account.Credentials.from_service_account_file(
    "/runner/env/service_account.json",
    scopes=["https://www.googleapis.com/auth/compute"]
)

compute = discovery.build("compute", "v1", credentials=creds)

def list_subnets(region):
    return compute.subnetworks().list(
        project=PROJECT,
        region=region
    ).execute().get("items", [])

def utilization(subnet):
    used = subnet.get("usedIpCount", 0)
    cidr = ipaddress.ip_network(subnet["ipCidrRange"])
    total = cidr.num_addresses
    return (used * 100) / total if total else 0

def next_free_cidr(base, prefix, used):
    for net in ipaddress.ip_network(base).subnets(new_prefix=prefix):
        if str(net) not in used:
            return str(net)
    return None

def main():
    regions = ["asia-south1", "asia-east1", "europe-west1", "us-central1"]
    result = []

    for region in regions:
        subnets = list_subnets(region)
        for s in subnets:
            if not s["name"].endswith("-snt"):
                continue

            util = utilization(s)
            if util < THRESHOLD:
                continue

            prefix = ipaddress.ip_network(s["ipCidrRange"]).prefixlen
            base = str(ipaddress.ip_network(s["ipCidrRange"]).supernet(new_prefix=16))
            used = [x["ipCidrRange"] for x in subnets]

            next_cidr = next_free_cidr(base, prefix, used)
            if not next_cidr:
                continue

            result.append({
                "region": region,
                "network": s["network"],
                "new_cidr": next_cidr,
                "base_name": s["name"].rsplit("-", 2)[0]
            })

    print(result)

if __name__ == "__main__":
    main()
