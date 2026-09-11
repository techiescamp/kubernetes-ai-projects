import json
import logging
from typing import Any, Dict, Optional

from langchain_core.tools import tool
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

# Load kubeconfig. In-cluster config is tried first since that's the expected path when running
# as a Deployment; local kubeconfig is the fallback for running the agent from a dev machine.
try:
    config.load_incluster_config()
except Exception:
    try:
        config.load_kube_config()
    except Exception as e:
        logger.error(f"Could not load any Kubernetes configuration (in-cluster or kubeconfig): {e}")

# Kinds the generic delete_resource/apply_kubernetes_yaml tools are allowed to touch. Broadened on
# explicit request after a real deployed test where the agent burned every remediation attempt
# re-proposing a StorageClass change that was refused here, with no path to ever succeed.
#
# Still deliberately EXCLUDES:
#   - Secret: never created/modified through these generic tools (the code-level guard in
#     get_resource/list_secrets keeps .data unreadable too) - see k8s/02-rbac.yaml.
#   - Role/ClusterRole/RoleBinding/ClusterRoleBinding: so the agent can never grant itself or
#     anything else more permission than it already has. This is the one boundary that stays
#     closed at BOTH layers (no rbac.authorization.k8s.io write in the ClusterRole either).
# Everything else the ClusterRole grants is allowed here. Note the real safety gate is the
# human-approval interrupt in agents.py - nothing in this set is applied without a person
# approving it first (unless REQUIRE_APPROVAL=false, which is opt-in and documented as risky).
_ALLOWED_KINDS = {
    "Pod", "Deployment", "Service", "ConfigMap", "PersistentVolumeClaim", "PersistentVolume",
    "Ingress", "Job", "CronJob", "StatefulSet", "DaemonSet", "Namespace",
    "StorageClass", "ReplicaSet", "HorizontalPodAutoscaler", "PodDisruptionBudget",
    "NetworkPolicy", "ServiceAccount", "Endpoints", "ResourceQuota", "LimitRange",
    "PriorityClass", "VolumeAttachment", "CustomResourceDefinition", "ReplicationController",
}
# Case-insensitive lookup -> canonical casing. A real deployed test showed a model calling
# delete_resource(kind='pod') (lowercase) - Kubernetes `kind` values are always PascalCase, but an
# LLM-generated free-text argument isn't guaranteed to match that exactly, so the exact-match check
# rejected a perfectly valid request. Resolve case-insensitively, then use the canonical value for
# everything downstream (API calls need the exact correct casing).
_ALLOWED_KINDS_CI = {k.lower(): k for k in _ALLOWED_KINDS}


def _api_error_message(e) -> str:
    """
    Pull the human-readable reason out of a Kubernetes ApiException.

    str(ApiException) dumps the status line, every HTTP response header, the raw JSON body AND a
    full Python traceback - hundreds of tokens where one sentence carries all the meaning. That
    whole wall gets fed back to the model as a tool result, which buries the actual cause (a real
    deployed test had "spec.accessModes: Required value" lost inside a traceback, and the model
    burned an extra remediation attempt as a result) and wastes context. Return just the API
    server's own 'message' field when it can be parsed, falling back to reason/status.
    """
    try:
        body = json.loads(e.body)
        msg = body.get("message")
        if msg:
            return f"{msg} (HTTP {e.status})"
    except Exception:
        pass
    return f"{getattr(e, 'reason', 'error')} (HTTP {getattr(e, 'status', '?')})"


@tool
def list_namespaces() -> str:
    """List all namespaces in the Kubernetes cluster."""
    try:
        v1 = client.CoreV1Api()
        namespaces = v1.list_namespace()
        ns_list = [ns.metadata.name for ns in namespaces.items]
        return json.dumps({"namespaces": ns_list})
    except Exception as e:
        return f"Error listing namespaces: {str(e)}"


@tool
def get_pod_status(namespace: Optional[str] = None) -> str:
    """
    Get the status, restart count, and ready state of pods. Omit 'namespace' (or pass None) to
    list pods across ALL namespaces; pass a specific namespace to scope to just that one.
    Useful for identifying crashing or pending pods anywhere in the cluster.
    """
    try:
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(namespace=namespace) if namespace else v1.list_pod_for_all_namespaces()
        pod_list = []
        for pod in pods.items:
            status = pod.status.phase
            restarts = 0
            ready = "False"
            container_statuses = pod.status.container_statuses or []
            for cs in container_statuses:
                restarts += cs.restart_count
                if cs.ready:
                    ready = "True"

            pod_list.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": status,
                "restarts": restarts,
                "ready": ready,
                "node": pod.spec.node_name
            })
        return json.dumps({"namespace": namespace or "*all*", "pods": pod_list}, indent=2)
    except Exception as e:
        return f"Error getting pod status: {str(e)}"


@tool
def get_pod_logs(pod_name: str, namespace: str, container: Optional[str] = None, tail_lines: int = 50, previous: bool = False) -> str:
    """
    Retrieve logs for a specific pod or container.
    Specify 'container' if the pod has multiple containers.
    Set 'previous' to True to get logs from the last crashed instance of the container
    (essential for diagnosing CrashLoopBackOff - the current container may be empty or
    mid-startup, while the previous instance has the actual crash error). If the previous
    instance's logs aren't available (a known containerd/kubelet limitation, not specific to any
    one crash), this automatically falls back to the current container's logs instead.
    """
    def _fetch(want_previous: bool) -> str:
        v1 = client.CoreV1Api()
        kwargs = {"namespace": namespace, "name": pod_name, "tail_lines": tail_lines, "previous": want_previous}
        if container:
            kwargs["container"] = container
        return v1.read_namespaced_pod_log(**kwargs)

    try:
        logs = _fetch(previous)
        # containerd/kubelet sometimes returns this as literal 200 OK "log content" instead of
        # raising an error, when the previous container's log file is no longer on disk - a real
        # deployed test caught the model taking this placeholder at face value and giving up
        # instead of falling back to the current container's logs (which often still has the
        # crash message, e.g. right after a fast crash-loop restart).
        if previous and logs.strip().startswith("unable to retrieve container logs"):
            current_logs = _fetch(False)
            return (
                "Note: previous container logs were unavailable (containerd/kubelet limitation), "
                "falling back to current container logs:\n\n" + current_logs
            )
        return logs
    except Exception as e:
        return f"Error reading logs for pod {pod_name} in namespace {namespace}: {str(e)}"


@tool
def describe_pod(pod_name: str, namespace: str) -> str:
    """
    Get detailed status for a pod: phase, conditions, per-container state (waiting/running/
    terminated reason), exit codes, restart counts, and resource requests/limits. Use this to
    find the exact root cause of a crash (e.g. OOMKilled, ImagePullBackOff, non-zero exit code)
    rather than just a high-level status summary.
    """
    try:
        v1 = client.CoreV1Api()
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)

        owner_refs = [
            {"kind": o.kind, "name": o.name}
            for o in (pod.metadata.owner_references or [])
        ]

        conditions = [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in (pod.status.conditions or [])
        ]

        containers = []
        statuses = pod.status.container_statuses or []
        spec_containers = {c.name: c for c in pod.spec.containers}
        for cs in statuses:
            state_kind, state_detail = "unknown", {}
            if cs.state.waiting:
                state_kind = "waiting"
                state_detail = {"reason": cs.state.waiting.reason, "message": cs.state.waiting.message}
            elif cs.state.running:
                state_kind = "running"
                state_detail = {"started_at": str(cs.state.running.started_at)}
            elif cs.state.terminated:
                state_kind = "terminated"
                state_detail = {
                    "reason": cs.state.terminated.reason,
                    "exit_code": cs.state.terminated.exit_code,
                    "message": cs.state.terminated.message,
                }

            last_state_kind, last_state_detail = None, {}
            if cs.last_state and cs.last_state.terminated:
                last_state_kind = "terminated"
                last_state_detail = {
                    "reason": cs.last_state.terminated.reason,
                    "exit_code": cs.last_state.terminated.exit_code,
                    "message": cs.last_state.terminated.message,
                }

            resources = {}
            spec = spec_containers.get(cs.name)
            if spec and spec.resources:
                resources = {
                    "requests": spec.resources.requests,
                    "limits": spec.resources.limits,
                }

            containers.append({
                "name": cs.name,
                "image": spec.image if spec else None,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "state": state_kind,
                "state_detail": state_detail,
                "last_terminated_state": last_state_kind and last_state_detail,
                "resources": resources,
            })

        return json.dumps({
            "pod": pod_name,
            "namespace": namespace,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "owner_references": owner_refs,
            "standalone_pod": len(owner_refs) == 0,
            "conditions": conditions,
            "containers": containers,
        }, indent=2, default=str)
    except Exception as e:
        return f"Error describing pod {pod_name} in namespace {namespace}: {str(e)}"


@tool
def get_pod_events(namespace: str) -> str:
    """
    Get events in the namespace to see warnings, errors, scheduling failures, etc.
    """
    try:
        v1 = client.CoreV1Api()
        events = v1.list_namespaced_event(namespace=namespace)
        event_list = []
        sorted_events = sorted(events.items, key=lambda x: x.metadata.creation_timestamp or "", reverse=True)
        for event in sorted_events[:20]:
            event_list.append({
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "object": f"{event.involved_object.kind}/{event.involved_object.name}",
                "timestamp": str(event.metadata.creation_timestamp)
            })
        return json.dumps({"events": event_list}, indent=2)
    except Exception as e:
        return f"Error getting events for namespace {namespace}: {str(e)}"


@tool
def get_cluster_events(namespace: Optional[str] = None) -> str:
    """
    Get the most recent warning/error events across the WHOLE cluster (all namespaces) if
    'namespace' is omitted, or for a single namespace if given. Use this when troubleshooting an
    issue that isn't scoped to one namespace yet (e.g. "what's wrong with the cluster right now"),
    or when the affected namespace isn't known.

    IMPORTANT: events are HISTORY, not current state - Kubernetes keeps them for roughly an hour
    AFTER the condition they describe, including after it has been fixed. Every event returned here
    is annotated with 'age' and with 'object_status', which is looked up live at call time:
    "GONE - object no longer exists" means the event is stale and must be ignored, and anything
    else is that object's current status, which is what you should actually reason about. Never
    report a problem based on an event alone.
    """
    try:
        v1 = client.CoreV1Api()
        if namespace:
            events = v1.list_namespaced_event(namespace=namespace).items
        else:
            events = v1.list_event_for_all_namespaces().items
        sorted_events = sorted(events, key=lambda x: x.metadata.creation_timestamp or "", reverse=True)

        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        # Live status is looked up once per distinct object, not per event (the same object
        # usually produces many repeated warnings), and capped so a noisy cluster can't turn this
        # into hundreds of API calls.
        status_cache: Dict[str, str] = {}
        lookup_budget = 25

        def _live_status(inv) -> str:
            nonlocal lookup_budget
            key = f"{inv.kind}/{inv.namespace}/{inv.name}"
            if key in status_cache:
                return status_cache[key]
            if lookup_budget <= 0:
                return "not checked (lookup budget exhausted) - verify with get_resource"
            lookup_budget -= 1
            try:
                from kubernetes import dynamic
                from kubernetes.client import api_client as _ac
                dyn = dynamic.DynamicClient(_ac.ApiClient())
                res = dyn.resources.get(api_version=inv.api_version or "v1", kind=inv.kind)
                obj = (res.get(name=inv.name, namespace=inv.namespace)
                       if getattr(res, "namespaced", True) and inv.namespace
                       else res.get(name=inv.name))
                status = getattr(obj, "status", None)
                phase = (status or {}).get("phase") if status else None
                result = f"EXISTS (phase={phase})" if phase else "EXISTS"
            except ApiException as e:
                result = "GONE - object no longer exists, this event is stale" if e.status == 404 \
                    else f"unknown ({e.status})"
            except Exception:
                result = "unknown - verify with get_resource"
            status_cache[key] = result
            return result

        event_list = []
        for event in sorted_events[:30]:
            inv = event.involved_object
            ts = event.metadata.creation_timestamp
            age_min = int((now - ts).total_seconds() // 60) if ts else None
            event_list.append({
                "namespace": event.metadata.namespace,
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "object": f"{inv.kind}/{inv.name}",
                "timestamp": str(ts),
                "age": f"{age_min}m ago" if age_min is not None else "unknown",
                "object_status": _live_status(inv),
            })
        return json.dumps({
            "WARNING": (
                "Events are HISTORICAL and are retained ~1h after the condition they describe, "
                "including after it was fixed. Do NOT report an issue based on an event alone. "
                "Ignore any event whose object_status says GONE. For every other event, confirm "
                "the problem is still real by reading the object itself (get_resource/describe_*) "
                "before reporting it."
            ),
            "events": event_list,
        }, indent=2)
    except Exception as e:
        return f"Error getting cluster events: {str(e)}"


@tool
def list_nodes() -> str:
    """
    List all nodes in the cluster with their readiness condition, roles, and capacity/allocatable
    resources. Use this to check overall cluster health or find a node causing scheduling issues.
    """
    try:
        v1 = client.CoreV1Api()
        nodes = v1.list_node()
        node_list = []
        for n in nodes.items:
            conditions = {c.type: c.status for c in (n.status.conditions or [])}
            roles = [
                label.split("/", 1)[1] for label in n.metadata.labels or {}
                if label.startswith("node-role.kubernetes.io/")
            ] or ["<none>"]
            node_list.append({
                "name": n.metadata.name,
                "roles": roles,
                "ready": conditions.get("Ready", "Unknown"),
                "memory_pressure": conditions.get("MemoryPressure", "Unknown"),
                "disk_pressure": conditions.get("DiskPressure", "Unknown"),
                "capacity": n.status.capacity,
                "allocatable": n.status.allocatable,
            })
        return json.dumps({"nodes": node_list}, indent=2, default=str)
    except Exception as e:
        return f"Error listing nodes: {str(e)}"


@tool
def describe_node(node_name: str) -> str:
    """
    Get full detail for a single node: all conditions, taints, and capacity/allocatable - use this
    to diagnose NotReady nodes, disk/memory pressure, or scheduling failures caused by taints.
    """
    try:
        v1 = client.CoreV1Api()
        n = v1.read_node(name=node_name)
        conditions = [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in (n.status.conditions or [])
        ]
        taints = [
            {"key": t.key, "value": t.value, "effect": t.effect}
            for t in (n.spec.taints or [])
        ]
        return json.dumps({
            "node": node_name,
            "conditions": conditions,
            "taints": taints,
            "capacity": n.status.capacity,
            "allocatable": n.status.allocatable,
            "unschedulable": bool(n.spec.unschedulable),
        }, indent=2, default=str)
    except Exception as e:
        return f"Error describing node {node_name}: {str(e)}"


@tool
def list_deployments(namespace: Optional[str] = None) -> str:
    """
    List Deployments with desired/ready/available replica counts and image. Omit 'namespace'
    (or pass None) to list across ALL namespaces; pass a specific namespace to scope to just that one.
    """
    try:
        apps_v1 = client.AppsV1Api()
        deployments = (apps_v1.list_namespaced_deployment(namespace=namespace) if namespace
                        else apps_v1.list_deployment_for_all_namespaces())
        result = []
        for d in deployments.items:
            images = [c.image for c in d.spec.template.spec.containers]
            result.append({
                "name": d.metadata.name,
                "namespace": d.metadata.namespace,
                "desired_replicas": d.spec.replicas,
                "ready_replicas": d.status.ready_replicas or 0,
                "available_replicas": d.status.available_replicas or 0,
                "updated_replicas": d.status.updated_replicas or 0,
                "images": images,
            })
        return json.dumps({"namespace": namespace or "*all*", "deployments": result}, indent=2)
    except Exception as e:
        return f"Error listing deployments: {str(e)}"


@tool
def describe_deployment(deployment_name: str, namespace: str) -> str:
    """
    Get full detail for a Deployment: replica counts, rollout conditions (e.g. Progressing,
    Available), update strategy, and revision. Use this to diagnose a stuck/failed rollout.
    """
    try:
        apps_v1 = client.AppsV1Api()
        d = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        conditions = [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in (d.status.conditions or [])
        ]
        images = [c.image for c in d.spec.template.spec.containers]
        return json.dumps({
            "name": deployment_name,
            "namespace": namespace,
            "desired_replicas": d.spec.replicas,
            "ready_replicas": d.status.ready_replicas or 0,
            "available_replicas": d.status.available_replicas or 0,
            "updated_replicas": d.status.updated_replicas or 0,
            "unavailable_replicas": d.status.unavailable_replicas or 0,
            "strategy": d.spec.strategy.type if d.spec.strategy else None,
            "images": images,
            "conditions": conditions,
        }, indent=2, default=str)
    except Exception as e:
        return f"Error describing deployment {deployment_name} in namespace {namespace}: {str(e)}"


@tool
def list_replicasets(namespace: Optional[str] = None) -> str:
    """
    List ReplicaSets with desired/current/ready counts - useful for spotting orphaned or old
    ReplicaSets left over from a rollout. Omit 'namespace' (or pass None) for ALL namespaces.
    """
    try:
        apps_v1 = client.AppsV1Api()
        rs_list = (apps_v1.list_namespaced_replica_set(namespace=namespace) if namespace
                   else apps_v1.list_replica_set_for_all_namespaces())
        result = [
            {
                "name": rs.metadata.name,
                "namespace": rs.metadata.namespace,
                "owner": [o.name for o in (rs.metadata.owner_references or [])],
                "desired": rs.spec.replicas,
                "current": rs.status.replicas or 0,
                "ready": rs.status.ready_replicas or 0,
            }
            for rs in rs_list.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "replicasets": result}, indent=2)
    except Exception as e:
        return f"Error listing replicasets: {str(e)}"


@tool
def list_services(namespace: Optional[str] = None) -> str:
    """
    List Services with type, cluster IP, ports, and selector. Omit 'namespace' (or pass None)
    for ALL namespaces.
    """
    try:
        v1 = client.CoreV1Api()
        services = (v1.list_namespaced_service(namespace=namespace) if namespace
                    else v1.list_service_for_all_namespaces())
        result = [
            {
                "name": s.metadata.name,
                "namespace": s.metadata.namespace,
                "type": s.spec.type,
                "cluster_ip": s.spec.cluster_ip,
                "ports": [{"port": p.port, "target_port": str(p.target_port), "protocol": p.protocol} for p in (s.spec.ports or [])],
                "selector": s.spec.selector,
            }
            for s in services.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "services": result}, indent=2)
    except Exception as e:
        return f"Error listing services: {str(e)}"


@tool
def list_ingresses(namespace: Optional[str] = None) -> str:
    """
    List Ingresses with hosts, backend services, and TLS configuration. Omit 'namespace' (or
    pass None) for ALL namespaces.
    """
    try:
        net_v1 = client.NetworkingV1Api()
        ingresses = (net_v1.list_namespaced_ingress(namespace=namespace) if namespace
                     else net_v1.list_ingress_for_all_namespaces())
        result = []
        for ing in ingresses.items:
            rules = []
            for r in (ing.spec.rules or []):
                paths = [
                    {"path": p.path, "backend_service": p.backend.service.name if p.backend.service else None}
                    for p in (r.http.paths if r.http else [])
                ]
                rules.append({"host": r.host, "paths": paths})
            result.append({
                "name": ing.metadata.name,
                "namespace": ing.metadata.namespace,
                "rules": rules,
                "tls_hosts": [h for t in (ing.spec.tls or []) for h in (t.hosts or [])],
            })
        return json.dumps({"namespace": namespace or "*all*", "ingresses": result}, indent=2)
    except Exception as e:
        return f"Error listing ingresses in namespace {namespace}: {str(e)}"


@tool
def list_configmaps(namespace: Optional[str] = None) -> str:
    """
    List ConfigMaps with their key names (not values) - for checking existence/wiring. Omit
    'namespace' (or pass None) for ALL namespaces.
    """
    try:
        v1 = client.CoreV1Api()
        cms = (v1.list_namespaced_config_map(namespace=namespace) if namespace
               else v1.list_config_map_for_all_namespaces())
        result = [
            {"name": cm.metadata.name, "namespace": cm.metadata.namespace, "keys": list((cm.data or {}).keys())}
            for cm in cms.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "configmaps": result}, indent=2)
    except Exception as e:
        return f"Error listing configmaps: {str(e)}"


@tool
def list_secrets(namespace: Optional[str] = None) -> str:
    """
    List Secrets with their name, type, and key NAMES only - values are never read or returned
    by this tool. Use this to confirm a Secret referenced by a pod actually exists and has the
    expected key. Omit 'namespace' (or pass None) for ALL namespaces.
    """
    try:
        v1 = client.CoreV1Api()
        secrets = (v1.list_namespaced_secret(namespace=namespace) if namespace
                   else v1.list_secret_for_all_namespaces())
        result = [
            {"name": s.metadata.name, "namespace": s.metadata.namespace, "type": s.type, "keys": list((s.data or {}).keys())}
            for s in secrets.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "secrets": result}, indent=2)
    except Exception as e:
        return f"Error listing secrets: {str(e)}"


@tool
def list_pvcs(namespace: Optional[str] = None) -> str:
    """
    List PersistentVolumeClaims with bound status, capacity, and storage class. Omit 'namespace'
    (or pass None) for ALL namespaces.
    """
    try:
        v1 = client.CoreV1Api()
        pvcs = (v1.list_namespaced_persistent_volume_claim(namespace=namespace) if namespace
                else v1.list_persistent_volume_claim_for_all_namespaces())
        result = [
            {
                "name": p.metadata.name,
                "namespace": p.metadata.namespace,
                "status": p.status.phase,
                "capacity": (p.status.capacity or {}).get("storage"),
                "storage_class": p.spec.storage_class_name,
                "volume": p.spec.volume_name,
            }
            for p in pvcs.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "pvcs": result}, indent=2)
    except Exception as e:
        return f"Error listing PVCs: {str(e)}"


@tool
def list_jobs(namespace: Optional[str] = None) -> str:
    """
    List Jobs with completion/failure/active counts. Omit 'namespace' (or pass None) for ALL
    namespaces.
    """
    try:
        batch_v1 = client.BatchV1Api()
        jobs = (batch_v1.list_namespaced_job(namespace=namespace) if namespace
                else batch_v1.list_job_for_all_namespaces())
        result = [
            {
                "name": j.metadata.name,
                "namespace": j.metadata.namespace,
                "active": j.status.active or 0,
                "succeeded": j.status.succeeded or 0,
                "failed": j.status.failed or 0,
            }
            for j in jobs.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "jobs": result}, indent=2)
    except Exception as e:
        return f"Error listing jobs: {str(e)}"


@tool
def list_cronjobs(namespace: Optional[str] = None) -> str:
    """
    List CronJobs with schedule, suspend state, and last schedule time. Omit 'namespace' (or
    pass None) for ALL namespaces.
    """
    try:
        batch_v1 = client.BatchV1Api()
        cronjobs = (batch_v1.list_namespaced_cron_job(namespace=namespace) if namespace
                    else batch_v1.list_cron_job_for_all_namespaces())
        result = [
            {
                "name": cj.metadata.name,
                "namespace": cj.metadata.namespace,
                "schedule": cj.spec.schedule,
                "suspended": bool(cj.spec.suspend),
                "last_schedule_time": str(cj.status.last_schedule_time) if cj.status.last_schedule_time else None,
            }
            for cj in cronjobs.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "cronjobs": result}, indent=2)
    except Exception as e:
        return f"Error listing cronjobs: {str(e)}"


@tool
def list_statefulsets(namespace: Optional[str] = None) -> str:
    """
    List StatefulSets with replica counts and update strategy. Omit 'namespace' (or pass None)
    for ALL namespaces.
    """
    try:
        apps_v1 = client.AppsV1Api()
        sts_list = (apps_v1.list_namespaced_stateful_set(namespace=namespace) if namespace
                    else apps_v1.list_stateful_set_for_all_namespaces())
        result = [
            {
                "name": sts.metadata.name,
                "namespace": sts.metadata.namespace,
                "desired_replicas": sts.spec.replicas,
                "ready_replicas": sts.status.ready_replicas or 0,
                "update_strategy": sts.spec.update_strategy.type if sts.spec.update_strategy else None,
            }
            for sts in sts_list.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "statefulsets": result}, indent=2)
    except Exception as e:
        return f"Error listing statefulsets: {str(e)}"


@tool
def list_daemonsets(namespace: Optional[str] = None) -> str:
    """
    List DaemonSets with desired/current/ready pod counts. Omit 'namespace' (or pass None) for
    ALL namespaces.
    """
    try:
        apps_v1 = client.AppsV1Api()
        ds_list = (apps_v1.list_namespaced_daemon_set(namespace=namespace) if namespace
                   else apps_v1.list_daemon_set_for_all_namespaces())
        result = [
            {
                "name": ds.metadata.name,
                "namespace": ds.metadata.namespace,
                "desired": ds.status.desired_number_scheduled,
                "current": ds.status.current_number_scheduled,
                "ready": ds.status.number_ready,
            }
            for ds in ds_list.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "daemonsets": result}, indent=2)
    except Exception as e:
        return f"Error listing daemonsets: {str(e)}"


@tool
def list_hpas(namespace: Optional[str] = None) -> str:
    """
    List HorizontalPodAutoscalers with current/target metrics and replica bounds. Omit
    'namespace' (or pass None) for ALL namespaces.
    """
    try:
        autoscaling_v2 = client.AutoscalingV2Api()
        hpas = (autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(namespace=namespace) if namespace
                else autoscaling_v2.list_horizontal_pod_autoscaler_for_all_namespaces())
        result = [
            {
                "name": h.metadata.name,
                "namespace": h.metadata.namespace,
                "target": f"{h.spec.scale_target_ref.kind}/{h.spec.scale_target_ref.name}",
                "min_replicas": h.spec.min_replicas,
                "max_replicas": h.spec.max_replicas,
                "current_replicas": h.status.current_replicas,
                "desired_replicas": h.status.desired_replicas,
            }
            for h in hpas.items
        ]
        return json.dumps({"namespace": namespace or "*all*", "hpas": result}, indent=2)
    except Exception as e:
        return f"Error listing HPAs: {str(e)}"


@tool
def get_resource_usage(namespace: Optional[str] = None) -> str:
    """
    Get live CPU/memory usage for pods (kubectl top pods equivalent) via the metrics.k8s.io API.
    Omit 'namespace' for all namespaces. Requires metrics-server to be installed in the cluster -
    returns a clear error if it isn't, rather than a raw stack trace.
    """
    try:
        custom_api = client.CustomObjectsApi()
        if namespace:
            usage = custom_api.list_namespaced_custom_object(
                group="metrics.k8s.io", version="v1beta1", namespace=namespace, plural="pods"
            )
        else:
            usage = custom_api.list_cluster_custom_object(
                group="metrics.k8s.io", version="v1beta1", plural="pods"
            )
        result = [
            {
                "pod": item["metadata"]["name"],
                "namespace": item["metadata"]["namespace"],
                "containers": [
                    {"name": c["name"], "cpu": c["usage"]["cpu"], "memory": c["usage"]["memory"]}
                    for c in item.get("containers", [])
                ],
            }
            for item in usage.get("items", [])
        ]
        return json.dumps({"pod_usage": result}, indent=2)
    except ApiException as e:
        if e.status == 404:
            return "Error: metrics-server is not installed in this cluster - resource usage data is unavailable."
        return f"Error getting resource usage: {str(e)}"
    except Exception as e:
        return f"Error getting resource usage: {str(e)}"


def _strip_noise(obj: dict) -> dict:
    """Removes routinely huge, low-diagnostic-value fields before returning an object to the LLM."""
    if not isinstance(obj, dict):
        return obj
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        meta.pop("managedFields", None)
    status = obj.get("status")
    if isinstance(status, dict):
        status.pop("images", None)  # a Node's cached-image list can be very large and rarely useful
    return obj


@tool
def get_resource(kind: str, name: Optional[str] = None, namespace: Optional[str] = None, api_version: Optional[str] = None) -> str:
    """
    Generic read tool for ANY Kubernetes resource kind - your fallback for anything without a
    dedicated tool above: NetworkPolicy, ResourceQuota, LimitRange, PodDisruptionBudget,
    ServiceAccount, Role/RoleBinding/ClusterRole/ClusterRoleBinding (read-only - never writable),
    Endpoints/EndpointSlice, StorageClass, custom resources/CRDs, or anything else. Do not assume a
    problem can't be checked just because there's no specific tool for it - reason about which
    Kubernetes object type is actually relevant (e.g. a ResourceQuota or LimitRange for "pod
    creation forbidden" errors, a NetworkPolicy for unexpected connectivity issues, a
    RoleBinding/ClusterRoleBinding for permission errors) and use this tool to inspect it.
    - Omit 'name' to list all resources of that kind (optionally scoped to 'namespace' for
      namespaced kinds) - returns names only, not full detail.
    - Provide 'name' for full detail on one resource (like `kubectl get <kind> <name> -o yaml`).
    - 'api_version' is optional for common built-in kinds (auto-discovered); specify it
      (e.g. 'networking.k8s.io/v1', 'example.com/v1alpha1') for custom resources or if ambiguous.
    - Secret values are never returned by this tool, even if requested - use list_secrets instead
      for Secret existence/key checks.
    """
    if kind.strip().lower() in ("secret", "secrets"):
        return "Error: use list_secrets instead - this tool never returns Secret data to keep that boundary consistent regardless of how it's asked for."
    try:
        from kubernetes import dynamic
        from kubernetes.client import api_client as _api_client

        dyn = dynamic.DynamicClient(_api_client.ApiClient())
        resource = dyn.resources.get(api_version=api_version, kind=kind) if api_version else dyn.resources.get(kind=kind)
        is_namespaced = bool(getattr(resource, "namespaced", True))

        if name:
            obj = resource.get(name=name, namespace=namespace) if is_namespaced else resource.get(name=name)
            body = _strip_noise(obj.to_dict())
            return json.dumps(body, indent=2, default=str)[:8000]

        objs = resource.get(namespace=namespace) if (is_namespaced and namespace) else resource.get()
        items = objs.to_dict().get("items", [])
        summary = [
            {"name": i.get("metadata", {}).get("name"), "namespace": i.get("metadata", {}).get("namespace")}
            for i in items
        ]
        return json.dumps({"kind": kind, "count": len(summary), "items": summary}, indent=2, default=str)
    except Exception as e:
        return f"Error getting {kind}{'/' + name if name else ''}: {str(e)}"


@tool
def restart_pod(pod_name: str, namespace: str) -> str:
    """
    Restart a pod by deleting it. Kubernetes will only recreate it automatically if it is
    owned by a Deployment/ReplicaSet/StatefulSet/DaemonSet (check describe_pod's
    'owner_references' / 'standalone_pod' fields first) - for a standalone pod (created via
    create_pod or bare YAML) this PERMANENTLY DELETES it with nothing to bring it back.
    Only use this for transient/config-external failures (e.g. the container crashed once and
    a fresh start should clear it). Do NOT use this to fix a bad container image, bad command,
    wrong env var, or any other error baked into the pod spec itself - deleting and recreating
    with the same spec will just fail the same way again (or delete a standalone pod for good).
    For a wrong/broken image, use update_pod_image instead. If the pod is owned by a Deployment,
    prefer rollout_restart_deployment instead of deleting the pod directly.
    """
    try:
        v1 = client.CoreV1Api()
        v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        return f"Successfully initiated restart (deletion) of pod '{pod_name}' in namespace '{namespace}'."
    except Exception as e:
        return f"Error deleting/restarting pod {pod_name}: {str(e)}"


@tool
def update_pod_image(pod_name: str, container_name: str, image: str, namespace: str) -> str:
    """
    Patch a container's image on an existing pod in place (no delete/recreate needed).
    Use this to fix ImagePullBackOff/ErrImagePull/wrong-tag errors on a STANDALONE pod. If the
    pod is owned by a Deployment, use patch_deployment_image instead so the fix survives future
    rollouts (a direct pod patch is overwritten the next time the Deployment's ReplicaSet
    reconciles).
    """
    try:
        v1 = client.CoreV1Api()
        patch = {"spec": {"containers": [{"name": container_name, "image": image}]}}
        v1.patch_namespaced_pod(name=pod_name, namespace=namespace, body=patch)
        return (
            f"Successfully updated container '{container_name}' in pod '{pod_name}' "
            f"(namespace '{namespace}') to image '{image}'."
        )
    except Exception as e:
        hint = ""
        if "may not add or remove containers" in str(e):
            # A container list patch with a name that doesn't match any existing container looks
            # like "add a container" to Kubernetes' strategic merge, which is forbidden on a
            # running pod - this is almost always a wrong container_name guess, not a real
            # add/remove attempt. Surface the pod's actual container names so the model can retry
            # with the correct one in the same turn instead of falling back to delete+recreate.
            try:
                pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                real_names = [c.name for c in pod.spec.containers]
                hint = (
                    f" Hint: container_name '{container_name}' does not match any container on "
                    f"this pod - its actual container name(s): {real_names}. Retry with the "
                    f"correct name."
                )
            except Exception:
                pass
        return f"Error updating image for pod {pod_name}, container {container_name}: {str(e)}{hint}"


def _real_container_names(apps_v1, deployment_name: str, namespace: str) -> list:
    """
    Reads a Deployment's actual container names. A strategic merge patch on
    spec.template.spec.containers matches list entries by 'name' - if the given name doesn't
    match any existing container, Kubernetes doesn't error, it silently APPENDS a new container
    entry instead (unlike the equivalent Pod-level patch, which correctly rejects this). A real
    deployed test caught exactly this: a wrong container_name guess quietly turned a one-container
    Deployment into a broken two-container one instead of failing loudly. Tools that patch the
    containers list call this first and refuse (with the real names) rather than risk that.
    """
    deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    return [c.name for c in deployment.spec.template.spec.containers]


@tool
def patch_deployment_image(deployment_name: str, container_name: str, image: str, namespace: str) -> str:
    """
    Patch a container's image on a Deployment. This is the correct fix for a bad/wrong image on
    a Deployment-managed pod (ImagePullBackOff/ErrImagePull/wrong tag) - unlike patching the pod
    directly, this change survives future rollouts and triggers a proper rolling update.
    """
    try:
        apps_v1 = client.AppsV1Api()
        real_names = _real_container_names(apps_v1, deployment_name, namespace)
        if container_name not in real_names:
            return (
                f"Error: container_name '{container_name}' does not match any container on "
                f"deployment '{deployment_name}' - its actual container name(s): {real_names}. "
                f"Retry with the correct name (a mismatched name would silently create an extra, "
                f"broken container rather than patch the existing one)."
            )
        patch = {"spec": {"template": {"spec": {"containers": [{"name": container_name, "image": image}]}}}}
        apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch)
        return (
            f"Successfully updated container '{container_name}' in deployment '{deployment_name}' "
            f"(namespace '{namespace}') to image '{image}'. Kubernetes will roll out new pods."
        )
    except Exception as e:
        return f"Error updating image for deployment {deployment_name}, container {container_name}: {str(e)}"


@tool
def patch_deployment(deployment_name: str, patch: Dict[str, Any], namespace: str) -> str:
    """
    Apply a Kubernetes strategic merge patch to a Deployment's spec - the general-purpose tool for
    any Deployment change that isn't an image update (use patch_deployment_image for that instead).
    Use this to attach a ConfigMap/Secret to a container as environment variables (envFrom) or
    individual values (env), mount one as a volume, add/change env vars, adjust resource
    requests/limits, add labels/annotations, etc.

    `patch` must be a dict matching the Deployment's structure under 'spec', e.g. to expose every
    key in ConfigMap 'color-cm' as environment variables on a container named 'color-app-container':
      {"spec": {"template": {"spec": {"containers": [
        {"name": "color-app-container", "envFrom": [{"configMapRef": {"name": "color-cm"}}]}
      ]}}}}
    containers is a list, matched by 'name' - only include the container(s) you're actually
    changing, you do not need to repeat every container on the pod. Every container 'name' you
    include MUST exactly match an existing container - an unrecognized name silently creates a new,
    broken container instead of patching the right one. This tool can only ADD/UPDATE containers by
    name, never remove one - if a Deployment has an extra container that needs to be removed
    entirely, use apply_kubernetes_yaml with the FULL corrected manifest instead. IMPORTANT: setting
    an annotation, or putting a ConfigMap/Secret's name into an unrelated field like 'image', does
    NOT attach it to anything - only envFrom/env/volumes (as shown above) actually wire a
    ConfigMap/Secret into a container. If a container already has an 'env'/'envFrom' entry pointing
    at the WRONG or missing ConfigMap/Secret, include a corrected (or empty list [], to remove it)
    'env'/'envFrom' in this patch - whatever value you give a field here becomes its new value,
    completely replacing what was there before (this tool does its own merge in Python rather than
    relying on the Kubernetes API's raw PATCH endpoint, specifically so 'env': [] reliably clears an
    existing list - Kubernetes' own patch semantics merge Env entries by name instead of replacing
    the list, which makes an empty list patch there a silent no-op, not a clear). Only including the
    NEW reference and omitting 'env'/'envFrom' leaves any existing bad entry in place, and
    Kubernetes refuses to start the container while any referenced ConfigMap/Secret - old or new -
    doesn't exist.
    """
    def _deep_merge(base: dict, override: dict) -> None:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                _deep_merge(base[key], value)
            else:
                base[key] = value

    try:
        apps_v1 = client.AppsV1Api()
        deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        current = client.ApiClient().sanitize_for_serialization(deployment)

        patch_copy = json.loads(json.dumps(patch))
        containers_patch = (
            patch_copy.get("spec", {}).get("template", {}).get("spec", {}).get("containers")
        )
        if containers_patch:
            real_names = [c["name"] for c in current["spec"]["template"]["spec"]["containers"]]
            bad_names = [c.get("name") for c in containers_patch if c.get("name") not in real_names]
            if bad_names:
                return (
                    f"Error: container name(s) {bad_names} in the patch don't match any container "
                    f"on deployment '{deployment_name}' - its actual container name(s): {real_names}. "
                    f"Retry with the correct name(s) (an unrecognized name would silently create an "
                    f"extra, broken container rather than patch the right one)."
                )
            for cpatch in containers_patch:
                for c in current["spec"]["template"]["spec"]["containers"]:
                    if c.get("name") == cpatch.get("name"):
                        _deep_merge(c, cpatch)
                        break
            # Remove containers from the generic merge below - already applied above by name,
            # rather than by list position, which the generic merge doesn't know how to do.
            del patch_copy["spec"]["template"]["spec"]["containers"]

        _deep_merge(current, patch_copy)
        apps_v1.replace_namespaced_deployment(name=deployment_name, namespace=namespace, body=current)
        return (
            f"Successfully patched deployment '{deployment_name}' (namespace '{namespace}') with "
            f"{json.dumps(patch)}. Kubernetes will roll out new pods reflecting the change."
        )
    except Exception as e:
        return f"Error patching deployment {deployment_name}: {str(e)}"


@tool
def rollout_restart_deployment(deployment_name: str, namespace: str) -> str:
    """
    Trigger a rolling restart of a Deployment (equivalent to `kubectl rollout restart`), by
    patching a restart timestamp annotation on its pod template. This is the correct fix for a
    Deployment-managed pod stuck in a transient crash loop where the spec itself is correct -
    unlike restart_pod (which only deletes one pod), this cleanly recreates all replicas via a
    normal rolling update.
    """
    try:
        import datetime
        apps_v1 = client.AppsV1Api()
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.datetime.utcnow().isoformat() + "Z"
                        }
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch)
        return f"Successfully triggered a rolling restart of deployment '{deployment_name}' in namespace '{namespace}'."
    except Exception as e:
        return f"Error restarting deployment {deployment_name}: {str(e)}"


@tool
def scale_deployment(deployment_name: str, replicas: int, namespace: str) -> str:
    """
    Scale a deployment to the specified number of replicas.
    """
    try:
        apps_v1 = client.AppsV1Api()
        deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        deployment.spec.replicas = replicas
        apps_v1.replace_namespaced_deployment(name=deployment_name, namespace=namespace, body=deployment)
        return f"Successfully scaled deployment '{deployment_name}' to {replicas} replicas."
    except Exception as e:
        return f"Error scaling deployment {deployment_name}: {str(e)}"


@tool
def apply_kubernetes_yaml(yaml_content: str, namespace: Optional[str] = None) -> str:
    """
    Apply raw YAML configuration to the cluster (creates resources, or updates them if they
    already exist). The yaml_content parameter should contain one or more valid YAML manifests
    (separated by '---' for multiple documents). Covers common workload, storage, networking and
    policy kinds (Pod, Deployment, Service, ConfigMap, PVC, PersistentVolume, StorageClass,
    Ingress, NetworkPolicy, Job, CronJob, StatefulSet, DaemonSet, HPA, PDB, ResourceQuota,
    LimitRange, Namespace, CRDs, ...) - Secrets and RBAC objects (Role/ClusterRole/Bindings) are
    never creatable/modifiable this way.
    For any NAMESPACED kind you MUST say which namespace it belongs in - either set
    metadata.namespace inside the manifest itself, or pass the namespace argument. This tool will
    refuse a namespaced manifest that specifies neither rather than guessing: silently falling back
    to "default" is how a real deployed test ended up creating a PVC in the 'default' namespace
    when the resource being troubleshot lived in 'max-ns', and then reporting that as a success.
    Cluster-scoped kinds (PersistentVolume, Namespace) ignore the namespace argument.
    Many Kubernetes kinds only allow specific fields to be changed in place once a resource
    exists, and reject everything else with a 422 "is invalid" / "field is immutable" /
    "may not change fields other than..." error - a Pod only allows its image (and a few other
    fields) to be patched (not resources/command/args/env); a PersistentVolumeClaim's spec is
    entirely immutable after creation except resources.requests. If a patch attempt through this
    tool is rejected for that reason: first delete_resource the object, then call this tool again
    with the FULL corrected spec (every field, not just the one you're changing) - once the old
    object is gone this creates fresh rather than patching, so fields that can't be patched apply
    fine on the recreate. Don't use create_pod for a pod recreate - it only supports image/port
    and will silently drop the rest of the spec.
    """
    import yaml
    from kubernetes import dynamic
    from kubernetes.client import api_client

    try:
        docs = [d for d in yaml.safe_load_all(yaml_content) if d]
        if not docs:
            return "Error: Provided YAML content is empty or invalid."

        dyn = dynamic.DynamicClient(api_client.ApiClient())
        results = []
        for doc in docs:
            kind = doc.get("kind")
            api_version = doc.get("apiVersion")
            name = doc.get("metadata", {}).get("name", "<unnamed>")

            if not kind or kind.lower() not in _ALLOWED_KINDS_CI:
                results.append(
                    f"{kind}/{name}: REFUSED - kind '{kind}' is not in the allowed set "
                    f"({sorted(_ALLOWED_KINDS)}). Secrets and RBAC objects "
                    f"(Role/ClusterRole/Bindings) can never be applied through this tool - do not "
                    f"retry this call, it will be refused identically every time. Achieve the goal "
                    f"another way, or report that it needs a human."
                )
                continue

            try:
                resource = dyn.resources.get(api_version=api_version, kind=kind)
                doc_namespace = doc.get("metadata", {}).get("namespace") or namespace
                is_namespaced = resource.namespaced if hasattr(resource, "namespaced") else kind != "Namespace"
                if is_namespaced and not doc_namespace:
                    # Never silently fall back to "default" - see the docstring. Guessing here
                    # creates the right object in the wrong place and still reports success.
                    results.append(
                        f"{kind}/{name}: REFUSED - no namespace given for a namespaced kind. Set "
                        f"metadata.namespace in the manifest (or pass the namespace argument) to "
                        f"the namespace this resource actually belongs in, then retry."
                    )
                    continue
                try:
                    if is_namespaced:
                        resource.create(body=doc, namespace=doc_namespace)
                    else:
                        resource.create(body=doc)
                    results.append(f"{kind}/{name}: created")
                except ApiException as e:
                    if e.status == 409:
                        try:
                            if is_namespaced:
                                resource.patch(name=name, namespace=doc_namespace, body=doc,
                                                content_type="application/merge-patch+json")
                            else:
                                resource.patch(name=name, body=doc,
                                                content_type="application/merge-patch+json")
                            results.append(f"{kind}/{name}: updated")
                        except ApiException as pe:
                            # The object already existed and the update was rejected - almost
                            # always an immutable field. Say so in one actionable line instead of
                            # surfacing a raw 422 that reads like the create failed.
                            results.append(
                                f"{kind}/{name}: FAILED - it already exists and could not be "
                                f"updated in place: {_api_error_message(pe)} "
                                f"To change an immutable field: read the object's CURRENT full spec "
                                f"with get_resource, delete_resource it, then apply the complete "
                                f"corrected manifest (every required field - for a PVC that means "
                                f"accessModes, resources.requests and storageClassName, not just "
                                f"the field you're changing)."
                            )
                    else:
                        raise
            except ApiException as e:
                results.append(f"{kind}/{name}: FAILED - {_api_error_message(e)}")
            except Exception as e:
                results.append(f"{kind}/{name}: FAILED - {str(e)}")

        return "\n".join(results)
    except Exception as e:
        return f"Error applying YAML: {str(e)}"


@tool
def create_namespace(namespace_name: str) -> str:
    """
    Create a new namespace in the Kubernetes cluster.
    """
    try:
        v1 = client.CoreV1Api()
        ns = client.V1Namespace(
            metadata=client.V1ObjectMeta(name=namespace_name)
        )
        v1.create_namespace(body=ns)
        return f"Successfully created namespace '{namespace_name}'."
    except Exception as e:
        return f"Error creating namespace '{namespace_name}': {str(e)}"


@tool
def create_pod(pod_name: str, image: str, namespace: str, container_port: Optional[int] = None) -> str:
    """
    Create a single bare pod with just a name, image, and optional port - nothing else
    (no resources/command/args/env/volumes). Use this ONLY for genuinely new ad-hoc/test pods with
    no other requirements. If the user does not specify an image, use a sensible default like
    'nginx:latest' or 'busybox:latest'.
    Do NOT use this to recreate an existing pod you just deleted (e.g. because a resource-limit or
    command/args change couldn't be patched in place) - this tool has no way to carry over the
    original command, args, env vars, or resource requests/limits, so the recreated pod silently
    loses them. For that case, use apply_kubernetes_yaml instead with the full corrected pod spec
    (image, command, args, resources, etc. all included) - after a delete, apply_kubernetes_yaml
    creates fresh rather than patching, so fields that can't be patched on a running pod (like
    resources or command) apply fine on the recreate.
    """
    try:
        v1 = client.CoreV1Api()
        container = client.V1Container(
            name=pod_name,
            image=image,
            ports=[client.V1ContainerPort(container_port=container_port)] if container_port else None,
        )
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name=pod_name),
            spec=client.V1PodSpec(containers=[container]),
        )
        v1.create_namespaced_pod(namespace=namespace, body=pod)
        return f"Successfully created pod '{pod_name}' (image: {image}) in namespace '{namespace}'."
    except Exception as e:
        return f"Error creating pod '{pod_name}' in namespace '{namespace}': {str(e)}"


@tool
def delete_resource(kind: str, name: str, namespace: Optional[str] = None) -> str:
    """
    Delete a resource by kind and name. Restricted to the same allowed kinds as
    apply_kubernetes_yaml (common workload, storage, networking and policy kinds - Secrets and
    RBAC objects are never allowed). Use for cleaning up ad-hoc/test resources created during
    remediation, or to recreate an object whose spec has immutable fields.
    For a NAMESPACED kind the namespace argument is required - this tool refuses to guess rather
    than defaulting to "default" and deleting the wrong object. Cluster-scoped kinds
    (PersistentVolume, Namespace) ignore it.
    Deleting is destructive and usually NOT the fix: never delete a resource that is currently
    healthy and in use just because something else referencing it is broken. A Bound
    PersistentVolume is refused outright - a real deployed test had the agent delete a Bound PV
    (destroying the storage binding a PVC depended on) while "troubleshooting" that PVC.
    """
    canonical_kind = _ALLOWED_KINDS_CI.get((kind or "").lower())
    if not canonical_kind:
        return f"Error: kind '{kind}' is not in the allowed set ({sorted(_ALLOWED_KINDS)})."
    kind = canonical_kind
    try:
        from kubernetes import dynamic
        from kubernetes.client import api_client

        dyn = dynamic.DynamicClient(api_client.ApiClient())
        # Resolve the kind through the cluster's own API discovery rather than a hardcoded
        # kind->apiVersion map. The old map covered only the original handful of kinds and would
        # KeyError outright on anything added to _ALLOWED_KINDS later (StorageClass, HPA, CRDs...),
        # and it would also silently pin the wrong version on a cluster serving a different one.
        api_version_hints = {
            "Pod": "v1", "Service": "v1", "ConfigMap": "v1", "ServiceAccount": "v1",
            "PersistentVolumeClaim": "v1", "PersistentVolume": "v1", "Namespace": "v1",
            "Endpoints": "v1", "ResourceQuota": "v1", "LimitRange": "v1",
            "ReplicationController": "v1",
            "Deployment": "apps/v1", "StatefulSet": "apps/v1", "DaemonSet": "apps/v1",
            "ReplicaSet": "apps/v1",
            "Job": "batch/v1", "CronJob": "batch/v1",
            "Ingress": "networking.k8s.io/v1", "NetworkPolicy": "networking.k8s.io/v1",
            "StorageClass": "storage.k8s.io/v1", "VolumeAttachment": "storage.k8s.io/v1",
        }
        hint = api_version_hints.get(kind)
        if hint:
            resource = dyn.resources.get(api_version=hint, kind=kind)
        else:
            # No hint (HPA, PDB, PriorityClass, CRDs, ...) - let discovery find it by kind alone.
            resource = dyn.resources.get(kind=kind)
        # Cluster-scoped kinds (Namespace, PersistentVolume - not just Namespace, which was the
        # only one handled before PersistentVolume was added) take no namespace argument at all;
        # ask the dynamic client's own discovery rather than hardcoding kind names again, so any
        # future _ALLOWED_KINDS addition doesn't need a matching update here too.
        is_namespaced = getattr(resource, "namespaced", True)
        if is_namespaced and not namespace:
            return (
                f"Error: no namespace given for namespaced kind '{kind}'. Specify the namespace "
                f"'{name}' actually lives in and retry - this tool will not default to 'default'."
            )

        if kind == "PersistentVolume":
            try:
                pv = resource.get(name=name)
                phase = (pv.status or {}).get("phase") if hasattr(pv, "status") else None
                if phase == "Bound":
                    claim = (pv.spec or {}).get("claimRef") or {}
                    return (
                        f"REFUSED: PersistentVolume '{name}' is currently Bound to "
                        f"{claim.get('namespace', '?')}/{claim.get('name', '?')} - deleting it "
                        f"would destroy storage that claim is actively using. If the goal is to "
                        f"fix that claim, fix the claim or create an ADDITIONAL PV that matches "
                        f"it; do not delete the one that is already working."
                    )
            except Exception:
                pass  # couldn't read it - fall through and let the delete itself report the error

        if is_namespaced:
            resource.delete(name=name, namespace=namespace)
        else:
            resource.delete(name=name)

        # Wait for the object to actually be gone, not just for the delete call to be accepted -
        # a real deployed test caught a race where a delete+recreate (via apply_kubernetes_yaml)
        # in the same turn hit the still-terminating old object (finalizer-blocked, e.g. a PVC
        # still referenced by a pod), got a 409/422, and failed the recreate. Deleting is
        # asynchronous in Kubernetes; a tool reporting "deleted" should mean it's actually gone.
        import time
        deadline = time.time() + 20
        still_terminating = False
        while time.time() < deadline:
            try:
                if is_namespaced:
                    resource.get(name=name, namespace=namespace)
                else:
                    resource.get(name=name)
                still_terminating = True
                time.sleep(1)
            except ApiException as e:
                if e.status == 404:
                    still_terminating = False
                    break
                raise
        else:
            still_terminating = True

        # Cluster-scoped objects have no namespace to report - saying "in namespace 'default'"
        # for a PersistentVolume (as this did before) is just wrong and misleads the model.
        ns_suffix = f" in namespace '{namespace}'" if is_namespaced else ""
        if still_terminating:
            return (
                f"Delete initiated for {kind}/{name}{ns_suffix}, but it is still terminating after "
                f"20s (likely blocked by a finalizer, e.g. another resource still referencing it - "
                f"for a PVC, check whether a pod still mounts it). Recreating it now would likely "
                f"fail or hit the old object - check again before retrying the create."
            )
        return f"Successfully deleted {kind}/{name}{ns_suffix} (confirmed gone)."
    except Exception as e:
        return f"Error deleting {kind}/{name}: {str(e)}"
