import json
import logging

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

# Kinds the generic delete_resource/apply_kubernetes_yaml tools are allowed to touch. Deliberately
# excludes RBAC objects (Role/ClusterRole/Bindings) and Secret data to prevent the agent from ever
# escalating its own privileges or reading credential material - matches the RBAC granted in
# k8s/02-rbac.yaml.
_ALLOWED_KINDS = {
    "Pod", "Deployment", "Service", "ConfigMap", "PersistentVolumeClaim",
    "Ingress", "Job", "CronJob", "StatefulSet", "DaemonSet", "Namespace",
}


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
def get_pod_status(namespace: str = "default") -> str:
    """
    Get the status, restart count, and ready state of all pods in a given namespace.
    Useful for identifying crashing or pending pods.
    """
    try:
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(namespace=namespace)
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
                "status": status,
                "restarts": restarts,
                "ready": ready,
                "node": pod.spec.node_name
            })
        return json.dumps({"namespace": namespace, "pods": pod_list}, indent=2)
    except Exception as e:
        return f"Error getting pods for namespace {namespace}: {str(e)}"


@tool
def get_pod_logs(pod_name: str, namespace: str = "default", container: str = None, tail_lines: int = 50, previous: bool = False) -> str:
    """
    Retrieve logs for a specific pod or container.
    Specify 'container' if the pod has multiple containers.
    Set 'previous' to True to get logs from the last crashed instance of the container
    (essential for diagnosing CrashLoopBackOff - the current container may be empty or
    mid-startup, while the previous instance has the actual crash error).
    """
    try:
        v1 = client.CoreV1Api()
        kwargs = {"namespace": namespace, "name": pod_name, "tail_lines": tail_lines, "previous": previous}
        if container:
            kwargs["container"] = container
        logs = v1.read_namespaced_pod_log(**kwargs)
        return logs
    except Exception as e:
        return f"Error reading logs for pod {pod_name} in namespace {namespace}: {str(e)}"


@tool
def describe_pod(pod_name: str, namespace: str = "default") -> str:
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
def get_pod_events(namespace: str = "default") -> str:
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
def get_cluster_events(namespace: str = None) -> str:
    """
    Get the most recent warning/error events across the WHOLE cluster (all namespaces) if
    'namespace' is omitted, or for a single namespace if given. Use this when troubleshooting an
    issue that isn't scoped to one namespace yet (e.g. "what's wrong with the cluster right now"),
    or when the affected namespace isn't known.
    """
    try:
        v1 = client.CoreV1Api()
        if namespace:
            events = v1.list_namespaced_event(namespace=namespace).items
        else:
            events = v1.list_event_for_all_namespaces().items
        sorted_events = sorted(events, key=lambda x: x.metadata.creation_timestamp or "", reverse=True)
        event_list = [
            {
                "namespace": event.metadata.namespace,
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "object": f"{event.involved_object.kind}/{event.involved_object.name}",
                "timestamp": str(event.metadata.creation_timestamp),
            }
            for event in sorted_events[:30]
        ]
        return json.dumps({"events": event_list}, indent=2)
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
def list_deployments(namespace: str = "default") -> str:
    """List Deployments in a namespace with desired/ready/available replica counts and image."""
    try:
        apps_v1 = client.AppsV1Api()
        deployments = apps_v1.list_namespaced_deployment(namespace=namespace)
        result = []
        for d in deployments.items:
            images = [c.image for c in d.spec.template.spec.containers]
            result.append({
                "name": d.metadata.name,
                "desired_replicas": d.spec.replicas,
                "ready_replicas": d.status.ready_replicas or 0,
                "available_replicas": d.status.available_replicas or 0,
                "updated_replicas": d.status.updated_replicas or 0,
                "images": images,
            })
        return json.dumps({"namespace": namespace, "deployments": result}, indent=2)
    except Exception as e:
        return f"Error listing deployments in namespace {namespace}: {str(e)}"


@tool
def describe_deployment(deployment_name: str, namespace: str = "default") -> str:
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
def list_replicasets(namespace: str = "default") -> str:
    """
    List ReplicaSets in a namespace with desired/current/ready counts - useful for spotting
    orphaned or old ReplicaSets left over from a rollout.
    """
    try:
        apps_v1 = client.AppsV1Api()
        rs_list = apps_v1.list_namespaced_replica_set(namespace=namespace)
        result = [
            {
                "name": rs.metadata.name,
                "owner": [o.name for o in (rs.metadata.owner_references or [])],
                "desired": rs.spec.replicas,
                "current": rs.status.replicas or 0,
                "ready": rs.status.ready_replicas or 0,
            }
            for rs in rs_list.items
        ]
        return json.dumps({"namespace": namespace, "replicasets": result}, indent=2)
    except Exception as e:
        return f"Error listing replicasets in namespace {namespace}: {str(e)}"


@tool
def list_services(namespace: str = "default") -> str:
    """List Services in a namespace with type, cluster IP, ports, and selector."""
    try:
        v1 = client.CoreV1Api()
        services = v1.list_namespaced_service(namespace=namespace)
        result = [
            {
                "name": s.metadata.name,
                "type": s.spec.type,
                "cluster_ip": s.spec.cluster_ip,
                "ports": [{"port": p.port, "target_port": str(p.target_port), "protocol": p.protocol} for p in (s.spec.ports or [])],
                "selector": s.spec.selector,
            }
            for s in services.items
        ]
        return json.dumps({"namespace": namespace, "services": result}, indent=2)
    except Exception as e:
        return f"Error listing services in namespace {namespace}: {str(e)}"


@tool
def list_ingresses(namespace: str = "default") -> str:
    """List Ingresses in a namespace with hosts, backend services, and TLS configuration."""
    try:
        net_v1 = client.NetworkingV1Api()
        ingresses = net_v1.list_namespaced_ingress(namespace=namespace)
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
                "rules": rules,
                "tls_hosts": [h for t in (ing.spec.tls or []) for h in (t.hosts or [])],
            })
        return json.dumps({"namespace": namespace, "ingresses": result}, indent=2)
    except Exception as e:
        return f"Error listing ingresses in namespace {namespace}: {str(e)}"


@tool
def list_configmaps(namespace: str = "default") -> str:
    """List ConfigMaps in a namespace with their key names (not values) - for checking existence/wiring."""
    try:
        v1 = client.CoreV1Api()
        cms = v1.list_namespaced_config_map(namespace=namespace)
        result = [{"name": cm.metadata.name, "keys": list((cm.data or {}).keys())} for cm in cms.items]
        return json.dumps({"namespace": namespace, "configmaps": result}, indent=2)
    except Exception as e:
        return f"Error listing configmaps in namespace {namespace}: {str(e)}"


@tool
def list_secrets(namespace: str = "default") -> str:
    """
    List Secrets in a namespace with their name, type, and key NAMES only - values are never
    read or returned by this tool. Use this to confirm a Secret referenced by a pod actually
    exists and has the expected key.
    """
    try:
        v1 = client.CoreV1Api()
        secrets = v1.list_namespaced_secret(namespace=namespace)
        result = [
            {"name": s.metadata.name, "type": s.type, "keys": list((s.data or {}).keys())}
            for s in secrets.items
        ]
        return json.dumps({"namespace": namespace, "secrets": result}, indent=2)
    except Exception as e:
        return f"Error listing secrets in namespace {namespace}: {str(e)}"


@tool
def list_pvcs(namespace: str = "default") -> str:
    """List PersistentVolumeClaims in a namespace with bound status, capacity, and storage class."""
    try:
        v1 = client.CoreV1Api()
        pvcs = v1.list_namespaced_persistent_volume_claim(namespace=namespace)
        result = [
            {
                "name": p.metadata.name,
                "status": p.status.phase,
                "capacity": (p.status.capacity or {}).get("storage"),
                "storage_class": p.spec.storage_class_name,
                "volume": p.spec.volume_name,
            }
            for p in pvcs.items
        ]
        return json.dumps({"namespace": namespace, "pvcs": result}, indent=2)
    except Exception as e:
        return f"Error listing PVCs in namespace {namespace}: {str(e)}"


@tool
def list_jobs(namespace: str = "default") -> str:
    """List Jobs in a namespace with completion/failure/active counts."""
    try:
        batch_v1 = client.BatchV1Api()
        jobs = batch_v1.list_namespaced_job(namespace=namespace)
        result = [
            {
                "name": j.metadata.name,
                "active": j.status.active or 0,
                "succeeded": j.status.succeeded or 0,
                "failed": j.status.failed or 0,
            }
            for j in jobs.items
        ]
        return json.dumps({"namespace": namespace, "jobs": result}, indent=2)
    except Exception as e:
        return f"Error listing jobs in namespace {namespace}: {str(e)}"


@tool
def list_cronjobs(namespace: str = "default") -> str:
    """List CronJobs in a namespace with schedule, suspend state, and last schedule time."""
    try:
        batch_v1 = client.BatchV1Api()
        cronjobs = batch_v1.list_namespaced_cron_job(namespace=namespace)
        result = [
            {
                "name": cj.metadata.name,
                "schedule": cj.spec.schedule,
                "suspended": bool(cj.spec.suspend),
                "last_schedule_time": str(cj.status.last_schedule_time) if cj.status.last_schedule_time else None,
            }
            for cj in cronjobs.items
        ]
        return json.dumps({"namespace": namespace, "cronjobs": result}, indent=2)
    except Exception as e:
        return f"Error listing cronjobs in namespace {namespace}: {str(e)}"


@tool
def list_statefulsets(namespace: str = "default") -> str:
    """List StatefulSets in a namespace with replica counts and update strategy."""
    try:
        apps_v1 = client.AppsV1Api()
        sts_list = apps_v1.list_namespaced_stateful_set(namespace=namespace)
        result = [
            {
                "name": sts.metadata.name,
                "desired_replicas": sts.spec.replicas,
                "ready_replicas": sts.status.ready_replicas or 0,
                "update_strategy": sts.spec.update_strategy.type if sts.spec.update_strategy else None,
            }
            for sts in sts_list.items
        ]
        return json.dumps({"namespace": namespace, "statefulsets": result}, indent=2)
    except Exception as e:
        return f"Error listing statefulsets in namespace {namespace}: {str(e)}"


@tool
def list_daemonsets(namespace: str = "default") -> str:
    """List DaemonSets in a namespace with desired/current/ready pod counts."""
    try:
        apps_v1 = client.AppsV1Api()
        ds_list = apps_v1.list_namespaced_daemon_set(namespace=namespace)
        result = [
            {
                "name": ds.metadata.name,
                "desired": ds.status.desired_number_scheduled,
                "current": ds.status.current_number_scheduled,
                "ready": ds.status.number_ready,
            }
            for ds in ds_list.items
        ]
        return json.dumps({"namespace": namespace, "daemonsets": result}, indent=2)
    except Exception as e:
        return f"Error listing daemonsets in namespace {namespace}: {str(e)}"


@tool
def list_hpas(namespace: str = "default") -> str:
    """List HorizontalPodAutoscalers in a namespace with current/target metrics and replica bounds."""
    try:
        autoscaling_v2 = client.AutoscalingV2Api()
        hpas = autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(namespace=namespace)
        result = [
            {
                "name": h.metadata.name,
                "target": f"{h.spec.scale_target_ref.kind}/{h.spec.scale_target_ref.name}",
                "min_replicas": h.spec.min_replicas,
                "max_replicas": h.spec.max_replicas,
                "current_replicas": h.status.current_replicas,
                "desired_replicas": h.status.desired_replicas,
            }
            for h in hpas.items
        ]
        return json.dumps({"namespace": namespace, "hpas": result}, indent=2)
    except Exception as e:
        return f"Error listing HPAs in namespace {namespace}: {str(e)}"


@tool
def get_resource_usage(namespace: str = None) -> str:
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


@tool
def restart_pod(pod_name: str, namespace: str = "default") -> str:
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
def update_pod_image(pod_name: str, container_name: str, image: str, namespace: str = "default") -> str:
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
        return f"Error updating image for pod {pod_name}, container {container_name}: {str(e)}"


@tool
def patch_deployment_image(deployment_name: str, container_name: str, image: str, namespace: str = "default") -> str:
    """
    Patch a container's image on a Deployment. This is the correct fix for a bad/wrong image on
    a Deployment-managed pod (ImagePullBackOff/ErrImagePull/wrong tag) - unlike patching the pod
    directly, this change survives future rollouts and triggers a proper rolling update.
    """
    try:
        apps_v1 = client.AppsV1Api()
        patch = {"spec": {"template": {"spec": {"containers": [{"name": container_name, "image": image}]}}}}
        apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch)
        return (
            f"Successfully updated container '{container_name}' in deployment '{deployment_name}' "
            f"(namespace '{namespace}') to image '{image}'. Kubernetes will roll out new pods."
        )
    except Exception as e:
        return f"Error updating image for deployment {deployment_name}, container {container_name}: {str(e)}"


@tool
def rollout_restart_deployment(deployment_name: str, namespace: str = "default") -> str:
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
def scale_deployment(deployment_name: str, replicas: int, namespace: str = "default") -> str:
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
def apply_kubernetes_yaml(yaml_content: str, namespace: str = "default") -> str:
    """
    Apply raw YAML configuration to the cluster (creates resources, or updates them if they
    already exist). The yaml_content parameter should contain one or more valid YAML manifests
    (separated by '---' for multiple documents). Restricted to common workload kinds (Pod,
    Deployment, Service, ConfigMap, PVC, Ingress, Job, CronJob, StatefulSet, DaemonSet,
    Namespace) - RBAC objects and Secret data cannot be created/modified this way.
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

            if kind not in _ALLOWED_KINDS:
                results.append(
                    f"{kind}/{name}: REFUSED - kind '{kind}' is not in the allowed set "
                    f"({sorted(_ALLOWED_KINDS)}). RBAC objects and other cluster-scoped/privileged "
                    f"kinds cannot be applied through this tool."
                )
                continue

            try:
                resource = dyn.resources.get(api_version=api_version, kind=kind)
                doc_namespace = doc.get("metadata", {}).get("namespace", namespace)
                is_namespaced = resource.namespaced if hasattr(resource, "namespaced") else kind != "Namespace"
                try:
                    if is_namespaced:
                        resource.create(body=doc, namespace=doc_namespace)
                    else:
                        resource.create(body=doc)
                    results.append(f"{kind}/{name}: created")
                except ApiException as e:
                    if e.status == 409:
                        if is_namespaced:
                            resource.patch(name=name, namespace=doc_namespace, body=doc,
                                            content_type="application/merge-patch+json")
                        else:
                            resource.patch(name=name, body=doc,
                                            content_type="application/merge-patch+json")
                        results.append(f"{kind}/{name}: updated")
                    else:
                        raise
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
def create_pod(pod_name: str, image: str, namespace: str = "default", container_port: int = None) -> str:
    """
    Create a single bare pod with the given name and container image in a namespace.
    Use this for ad-hoc/test pods. If the user does not specify an image, use a sensible
    default like 'nginx:latest' or 'busybox:latest'.
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
def delete_resource(kind: str, name: str, namespace: str = "default") -> str:
    """
    Delete a resource by kind and name. Restricted to the same allowed kinds as
    apply_kubernetes_yaml (Pod, Deployment, Service, ConfigMap, PVC, Ingress, Job, CronJob,
    StatefulSet, DaemonSet, Namespace). Use for cleaning up ad-hoc/test resources created during
    remediation.
    """
    if kind not in _ALLOWED_KINDS:
        return f"Error: kind '{kind}' is not in the allowed set ({sorted(_ALLOWED_KINDS)})."
    try:
        from kubernetes import dynamic
        from kubernetes.client import api_client

        dyn = dynamic.DynamicClient(api_client.ApiClient())
        # Core/apps/batch/networking v1 covers every kind in _ALLOWED_KINDS.
        api_version_map = {
            "Pod": "v1", "Service": "v1", "ConfigMap": "v1",
            "PersistentVolumeClaim": "v1", "Namespace": "v1",
            "Deployment": "apps/v1", "StatefulSet": "apps/v1", "DaemonSet": "apps/v1",
            "Job": "batch/v1", "CronJob": "batch/v1",
            "Ingress": "networking.k8s.io/v1",
        }
        resource = dyn.resources.get(api_version=api_version_map[kind], kind=kind)
        if kind == "Namespace":
            resource.delete(name=name)
        else:
            resource.delete(name=name, namespace=namespace)
        return f"Successfully deleted {kind}/{name}" + (f" in namespace '{namespace}'" if kind != "Namespace" else "") + "."
    except Exception as e:
        return f"Error deleting {kind}/{name}: {str(e)}"
