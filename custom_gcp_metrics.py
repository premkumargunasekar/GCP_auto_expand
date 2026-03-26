#!/usr/bin/python3
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function
__metaclass__ = type

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ansible.module_utils.basic import AnsibleModule

try:
    from google.cloud import monitoring_v3
    from google.oauth2 import service_account
    from google.api_core import retry as api_retry
    from google.api_core.exceptions import GoogleAPICallError
    from googleapiclient.discovery import build as gapi_build
    import google.auth
    HAS_GOOGLE_LIBS = True
    GOOGLE_IMPORT_ERROR = None
except ImportError as exc:
    HAS_GOOGLE_LIBS = False
    GOOGLE_IMPORT_ERROR = str(exc)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("gcp_metrics")
logging.basicConfig(
    level=os.environ.get("ANSIBLE_GCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
API_TIMEOUT = 60

# Exponential-backoff retry for transient GCP errors (429, 503, etc.)
_RETRY = api_retry.Retry(
    initial=1.0,
    maximum=30.0,
    multiplier=2.0,
    deadline=120.0,
    predicate=api_retry.if_transient_error,
)

# Metric type substrings that are gauges — must NOT use ALIGN_RATE
_GAUGE_METRIC_SUBSTRINGS = (
    "session_up",
    "received_routes_count",
    "peer_routes_up_count",
    "allocated_ports",
    "operational_status",
)

# Labels checked in priority order when naming a resource
_RESOURCE_LABEL_PRIORITY = (
    "attachment_name",
    "interconnect_attachment",
    "link_id",
    "router_id",
    "instance_id",
    "gateway_name",
    "vpn_gateway_name",
    "network_name",
    "project_id",
)

# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------
_GCP_SCOPES = [
    "https://www.googleapis.com/auth/monitoring.read",
    "https://www.googleapis.com/auth/compute.readonly",
]


def _resolve_sa_path(raw: str) -> str:
    """
    Resolve a service_account value that may contain an env-var reference.

    Accepts:
      - Literal path:  /etc/ansible/keys/gp-lab.json
      - Env-var ref:   ${GP_LAB_SA_KEY}

    Raises ValueError with a clear message when the variable is not set.
    """
    raw = (raw or "").strip()
    if raw.startswith("${") and raw.endswith("}"):
        var_name = raw[2:-1]
        resolved = os.environ.get(var_name)
        if not resolved:
            raise ValueError(
                f"Environment variable '{var_name}' is referenced in projects config "
                f"but is not set. Export it before running the playbook."
            )
        return resolved
    return raw


def get_credentials(project: dict):
    """
    Build GCP credentials for a project entry.

    Priority:
      1. service_account key (literal path or ${ENV_VAR})
      2. Application Default Credentials (GCE / Workload Identity / gcloud auth)
    """
    raw = project.get("service_account", "").strip()
    if raw:
        path = _resolve_sa_path(raw)
        log.debug("Project %s: using service account %s", project.get("project_id"), path)
        return service_account.Credentials.from_service_account_file(
            path, scopes=_GCP_SCOPES
        )

    log.debug(
        "Project %s: using Application Default Credentials",
        project.get("project_id"),
    )
    creds, _ = google.auth.default(scopes=_GCP_SCOPES)
    return creds


# ---------------------------------------------------------------------------
# Resource label → human-readable name
# ---------------------------------------------------------------------------
def get_resource_name(labels: dict) -> str:
    for key in _RESOURCE_LABEL_PRIORITY:
        val = labels.get(key)
        if val:
            return val
    return "unknown"


# ---------------------------------------------------------------------------
# Unit conversion  (applied AFTER fetch, not inside fetch)
# ---------------------------------------------------------------------------
def _convert(raw_val: float, unit: str) -> float:
    """Convert a raw GCP value to the display unit declared in metrics.yml."""
    unit = (unit or "mbps").lower()
    if unit == "mbps":
        return (raw_val * 8) / (1024 * 1024)
    if unit == "kbps":
        return (raw_val * 8) / 1024
    # "count" or anything else — no conversion
    return raw_val


# ---------------------------------------------------------------------------
# Aggregation aligner selection
# ---------------------------------------------------------------------------
def _choose_aligner(metric_type: str):
    """
    Return the correct per-series aligner for a given metric type.

    Rate metrics (byte counters)  → ALIGN_RATE
    Gauge metrics (session_up, allocated_ports, route counts) → ALIGN_MEAN
    """
    mt_lower = metric_type.lower()
    for substr in _GAUGE_METRIC_SUBSTRINGS:
        if substr in mt_lower:
            return monitoring_v3.Aggregation.Aligner.ALIGN_MEAN
    return monitoring_v3.Aggregation.Aligner.ALIGN_RATE


# ---------------------------------------------------------------------------
# Fetch raw time-series  (NO unit conversion — raw bytes/counts only)
# ---------------------------------------------------------------------------
def fetch_metric(
    client,
    project_id: str,
    metric_type: str,
    window_seconds: int,
) -> list:
    now = int(time.time())

    results = client.list_time_series(
        request={
            "name":   f"projects/{project_id}",
            "filter": f'metric.type="{metric_type}"',
            "interval": {
                "end_time":   {"seconds": now},
                "start_time": {"seconds": now - window_seconds},
            },
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            "aggregation": {
                "alignment_period":   {"seconds": 60},
                "per_series_aligner": _choose_aligner(metric_type),
            },
        },
        retry=_RETRY,
        timeout=API_TIMEOUT,
    )

    resource_data = []

    for ts in results:
        labels   = dict(ts.resource.labels)
        resource = get_resource_name(labels)
        values   = []

        for p in ts.points:
            if p.value.HasField("double_value"):
                values.append(p.value.double_value)
            elif p.value.HasField("int64_value"):
                values.append(float(p.value.int64_value))

        if not values:
            continue

        resource_data.append({
            "resource":   resource,
            "raw_avg":    sum(values) / len(values),
            "raw_max":    max(values),
            "raw_latest": values[0],
            "has_zero":   0.0 in values,
        })

    return resource_data


# ---------------------------------------------------------------------------
# Pick aggregated raw value from a resource dict
# ---------------------------------------------------------------------------
def _pick_raw(item: dict, agg: str) -> float:
    if agg == "avg":
        return item["raw_avg"]
    if agg == "max":
        return item["raw_max"]
    return item["raw_latest"]


# ---------------------------------------------------------------------------
# Status evaluation — GREEN / YELLOW / RED / NO_DATA
# ---------------------------------------------------------------------------
def evaluate(resource_data: list, meta: dict) -> str:
    is_crc = meta.get("is_crc_error", False)

    if not resource_data:
        return "GREEN" if is_crc else "NO_DATA"

    agg       = meta.get("aggregation", "avg")
    unit      = meta.get("unit", "mbps")
    threshold = meta.get("threshold")
    warning   = meta.get("warning")
    critical  = meta.get("critical", False)

    worst = "GREEN"

    for item in resource_data:
        val = _convert(_pick_raw(item, agg), unit)

        if is_crc:
            if val > 0:
                return "RED"
            continue

        if critical and val == 0:
            return "RED"

        if threshold is not None and val > threshold:
            return "RED"

        if warning is not None and val > warning:
            worst = "YELLOW"

    return worst


# ---------------------------------------------------------------------------
# Format per-resource output entry
# ---------------------------------------------------------------------------
def _format_resource(item: dict, meta: dict) -> dict:
    agg       = meta.get("aggregation", "avg")
    unit      = meta.get("unit", "mbps")
    threshold = meta.get("threshold")
    critical  = meta.get("critical", False)
    is_crc    = meta.get("is_crc_error", False)

    val = _convert(_pick_raw(item, agg), unit)

    if is_crc:
        status = "RED" if val > 0 else "GREEN"
    elif critical and val == 0:
        status = "RED"
    elif threshold is not None and val > threshold:
        status = "RED"
    else:
        status = "GREEN"

    return {
        "resource": item["resource"],
        "value":    round(val, 2),
        "status":   status,
    }


# ---------------------------------------------------------------------------
# VPC Peering via Compute API
# ---------------------------------------------------------------------------
def get_vpc_peering(project_id: str, credentials) -> list:
    try:
        svc  = gapi_build("compute", "v1", credentials=credentials)
        nets = svc.networks().list(project=project_id).execute()

        peerings = []
        for net in nets.get("items", []):
            for peer in net.get("peerings", []):
                state = peer.get("state", "UNKNOWN")
                peerings.append({
                    "resource": peer.get("name", "unknown"),
                    "value":    state,
                    "status":   "GREEN" if state == "ACTIVE" else "RED",
                })
        return peerings

    except Exception as exc:
        log.warning("Project %s: VPC peering fetch failed: %s", project_id, exc)
        return []


# ---------------------------------------------------------------------------
# Per-project processor
# ---------------------------------------------------------------------------
def process_project(project: dict, metrics: dict, windows: dict) -> dict:
    project_id = project.get("project_id", "unknown")

    # Build client — isolated so one bad project does not kill others
    try:
        creds  = get_credentials(project)
        client = monitoring_v3.MetricServiceClient(credentials=creds)
    except Exception as exc:
        log.error("Project %s: failed to initialise client: %s", project_id, exc)
        return {"project_id": project_id, "error": str(exc), "metrics": {}}

    result = {"project_id": project_id, "metrics": {}}

    # Annotate each metric with derived flags (done once, reused per window)
    annotated = {}
    for category, mset in metrics.items():
        annotated[category] = {}
        for name, raw_meta in mset.items():
            m = dict(raw_meta)
            m["is_crc_error"] = (name == "crc_error_count")
            annotated[category][name] = m

    # Collect monitoring metrics
    for category, mset in annotated.items():
        result["metrics"][category] = {}

        for name, meta in mset.items():
            result["metrics"][category][name] = {}

            for window_label, window_seconds in windows.items():
                try:
                    resource_data = fetch_metric(
                        client, project_id, meta["type"], window_seconds
                    )
                except GoogleAPICallError as exc:
                    log.warning(
                        "Project %s | %s.%s | %s: API error: %s",
                        project_id, category, name, window_label, exc,
                    )
                    result["metrics"][category][name][window_label] = {
                        "value":  None,
                        "status": "NO_DATA",
                        "error":  str(exc),
                    }
                    continue
                except Exception as exc:
                    log.error(
                        "Project %s | %s.%s | %s: unexpected error: %s",
                        project_id, category, name, window_label, exc,
                    )
                    result["metrics"][category][name][window_label] = {
                        "value":  None,
                        "status": "NO_DATA",
                        "error":  str(exc),
                    }
                    continue

                status    = evaluate(resource_data, meta)
                formatted = [_format_resource(item, meta) for item in resource_data]

                result["metrics"][category][name][window_label] = {
                    "value":  formatted if formatted else None,
                    "status": status,
                }

                log.debug(
                    "Project %s | %s.%s | %s → %s (%d resources)",
                    project_id, category, name, window_label,
                    status, len(formatted),
                )


    vpc_entries = get_vpc_peering(project_id, creds)

    if vpc_entries:
        vpc_status = (
            "GREEN"
            if all(p["status"] == "GREEN" for p in vpc_entries)
            else "RED"
        )
    else:
        vpc_status = "NO_DATA"

    for window_label in windows:
        result["metrics"].setdefault("vpc_peering", {})[window_label] = {
            "value":  vpc_entries if vpc_entries else None,
            "status": vpc_status,
        }

    log.info("Finished project: %s", project_id)
    return result


# ---------------------------------------------------------------------------
# Ansible module entry point
# ---------------------------------------------------------------------------
def run_module():
    module = AnsibleModule(
        argument_spec=dict(
            projects=   dict(type="list", elements="dict", required=True),
            metrics=    dict(type="dict", required=True),
            windows=    dict(type="dict", required=True),
            max_workers=dict(type="int",  default=5),
        ),
        supports_check_mode=True,
    )

    if not HAS_GOOGLE_LIBS:
        module.fail_json(
            msg=(
                "Required Google libraries are not installed. "
                "Install with: pip install google-cloud-monitoring "
                "google-auth google-api-python-client. "
                f"Import error: {GOOGLE_IMPORT_ERROR}"
            )
        )

    if module.check_mode:
        module.exit_json(changed=False, data=[], msg="Check mode — no API calls made.")

    projects    = module.params["projects"]
    metrics     = module.params["metrics"]
    windows     = module.params["windows"]
    max_workers = int(
        os.environ.get("GCP_MONITOR_WORKERS", module.params["max_workers"])
    )

    if not projects:
        module.fail_json(msg="'projects' list is empty.")
    if not metrics:
        module.fail_json(msg="'metrics' dict is empty.")
    if not windows:
        module.fail_json(msg="'windows' dict is empty.")

    results = []
    errors  = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(process_project, p, metrics, windows): p.get("project_id", "unknown")
            for p in projects
        }
        for future in as_completed(future_map):
            project_id = future_map[future]
            try:
                result = future.result()
                results.append(result)
                if "error" in result:
                    errors.append(f"{project_id}: {result['error']}")
            except Exception as exc:
                log.error(
                    "Unhandled exception in future for project %s: %s",
                    project_id, exc,
                )
                results.append({
                    "project_id": project_id,
                    "error":      str(exc),
                    "metrics":    {},
                })
                errors.append(f"{project_id}: {exc}")

    module.exit_json(
        changed=False,
        data=results,
        warnings=errors if errors else [],
    )


def main():
    run_module()


if __name__ == "__main__":
    main()
