import json
from typing import Any, Dict, Optional

from langchain_core.tools import tool
from kubernetes import client
from kubernetes.client.rest import ApiException

from .kube import _api_error_message

@tool
def check_permission(verb: str, resource: str, service_account: str, namespace: str,
                     api_group: str = "", resource_name: Optional[str] = None) -> str:
    """
    Answer "can this ServiceAccount do X?" - the API equivalent of
    `kubectl auth can-i <verb> <resource> --as=system:serviceaccount:<namespace>:<service_account>`.
    Use it for ANY question about permissions/authorization/RBAC, and ALWAYS use it to verify an
    RBAC fix: a Role can list the right verb and still not authorize anything if the apiGroup is
    wrong or no RoleBinding ties it to the ServiceAccount. Checking the Role's rules alone is not
    proof - this is.

    api_group matters and is easy to get wrong: deployments/statefulsets/daemonsets/replicasets
    are "apps"; jobs/cronjobs are "batch"; pods/services/configmaps/secrets are "" (core);
    ingresses/networkpolicies are "networking.k8s.io". A rule with the wrong apiGroup grants
    nothing, which is exactly the kind of silent failure this tool catches.
    """
    try:
        auth = client.AuthorizationV1Api()
        review = client.V1SubjectAccessReview(
            spec=client.V1SubjectAccessReviewSpec(
                user=f"system:serviceaccount:{namespace}:{service_account}",
                resource_attributes=client.V1ResourceAttributes(
                    namespace=namespace,
                    verb=verb,
                    group=api_group or "",
                    resource=resource,
                    name=resource_name,
                ),
            )
        )
        result = auth.create_subject_access_review(body=review)
        allowed = bool(result.status.allowed)
        detail = result.status.reason or ""
        who = f"system:serviceaccount:{namespace}:{service_account}"
        group_label = api_group or "core"
        return json.dumps({
            "allowed": allowed,
            "answer": "yes" if allowed else "no",
            "subject": who,
            "checked": f"{verb} {resource} (apiGroup: {group_label}) in namespace {namespace}",
            "reason": detail,
            "note": "" if allowed else (
                "Denied. Check that a Role/ClusterRole grants this verb on this resource with the "
                "CORRECT apiGroup, and that a RoleBinding/ClusterRoleBinding binds it to this "
                "ServiceAccount."
            ),
        }, indent=2)
    except Exception as e:
        return f"Error checking permission for {service_account}: {str(e)}"


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
    if not isinstance(obj, dict):
        return obj
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        meta.pop("managedFields", None)
    status = obj.get("status")
    if isinstance(status, dict):
        status.pop("images", None)
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
        return (
            "Error: reading Secrets is not permitted. Use list_secrets to check which Secrets and "
            "key names exist (never their values). You can still CREATE or REPLACE a Secret via "
            "apply_kubernetes_yaml if the fix needs one."
        )
    try:
        from kubernetes import dynamic
        from kubernetes.client import api_client as _api_client

        dyn = dynamic.DynamicClient(_api_client.ApiClient())
        resource = dyn.resources.get(api_version=api_version, kind=kind) if api_version else dyn.resources.get(kind=kind)
        is_namespaced = bool(getattr(resource, "namespaced", True))

        if name:
            obj = resource.get(name=name, namespace=namespace) if is_namespaced else resource.get(name=name)
            body = _strip_noise(obj.to_dict())
            if isinstance(body.get("data"), dict) and (body.get("kind") or "").lower() == "secret":
                body["data"] = {k: "<redacted>" for k in body["data"]}
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


