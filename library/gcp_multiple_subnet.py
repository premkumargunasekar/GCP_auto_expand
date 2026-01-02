#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule
import ipaddress
import subprocess
import json
import re

def run_module():
    module_args = dict(
        block=dict(type='str', required=True),
        subnet_prefix=dict(type='int', required=True),
        project=dict(type='str', required=True),
        region=dict(type='str', required=True),
        name_prefix=dict(type='str', required=True),
    )

    result = dict(
        changed=False,
        cidr=None,
        seq=None,
    )

    module = AnsibleModule(argument_spec=module_args, supports_check_mode=False)

    try:
        block = ipaddress.ip_network(module.params['block'])
        subnet_prefix = module.params['subnet_prefix']
        project = module.params['project']
        region = module.params['region']
        name_prefix = module.params['name_prefix']

        # Fetch LIVE GCP subnets
        cmd = [
            "gcloud", "compute", "networks", "subnets", "list",
            "--project", project,
            "--regions", region,
            "--format=json"
        ]

        out = subprocess.check_output(cmd)
        subnets = json.loads(out)

        used_networks = set()
        used_seqs = set()

        seq_re = re.compile(re.escape(name_prefix) + r"(\d+)-snt$")

        for s in subnets:
            used_networks.add(ipaddress.ip_network(s["ipCidrRange"]))
            m = seq_re.match(s["name"])
            if m:
                used_seqs.add(int(m.group(1)))

        def next_free_seq(existing):
            i = 1
            while i in existing:
                i += 1
            return i

        for candidate in block.subnets(new_prefix=subnet_prefix):
            if any(candidate.overlaps(u) for u in used_networks):
                continue

            seq = next_free_seq(used_seqs)
            result['cidr'] = str(candidate)
            result['seq'] = f"{seq:03d}"
            result['changed'] = True
            module.exit_json(**result)

        module.fail_json(msg="NO_AVAILABLE_SUBNET", **result)

    except Exception as e:
        module.fail_json(msg=str(e), **result)

def main():
    run_module()

if __name__ == '__main__':
    main()
