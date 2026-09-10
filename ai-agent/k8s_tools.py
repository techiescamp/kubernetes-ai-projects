import json
from langchain_core.tools import tool
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# Load kubeconfig
try:
    config.load_kube_config()
except Exception as e:
    # Fallback to in-cluster config if running inside a pod, or print warning
    try:
        config.load_incluster_config()
    except Exception:
        print("Warning: Could not load kubernetes configuration. Make sure kubeconfig is set up.")

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
        # Sort events by last timestamp
        sorted_events = sorted(events.items, key=lambda x: x.metadata.creation_timestamp or "", reverse=True)
        # Get top 20 events
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
    For a wrong/broken image, use update_pod_image instead.
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
    Use this to fix ImagePullBackOff/ErrImagePull/wrong-tag errors - container images are
    mutable on a running pod, so this is the correct fix instead of restart_pod.
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
def scale_deployment(deployment_name: str, replicas: int, namespace: str = "default") -> str:
    """
    Scale a deployment to the specified number of replicas.
    """
    try:
        apps_v1 = client.AppsV1Api()
        # Retrieve deployment
        deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        deployment.spec.replicas = replicas
        # Update deployment
        apps_v1.replace_namespaced_deployment(name=deployment_name, namespace=namespace, body=deployment)
        return f"Successfully scaled deployment '{deployment_name}' to {replicas} replicas."
    except Exception as e:
        return f"Error scaling deployment {deployment_name}: {str(e)}"

@tool
def apply_kubernetes_yaml(yaml_content: str, namespace: str = "default") -> str:
    """
    Apply raw YAML configuration to the cluster (creates or updates resources).
    The yaml_content parameter should contain the valid YAML manifest.
    """
    import yaml
    import tempfile
    from kubernetes import utils
    
    try:
        k8s_client = client.ApiClient()
        # Parse yaml
        docs = list(yaml.safe_load_all(yaml_content))
        if not docs or docs == [None]:
            return "Error: Provided YAML content is empty or invalid."
            
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.yaml', delete=False) as f:
            yaml.dump_all(docs, f)
            temp_path = f.name
            
        try:
            utils.create_from_yaml(k8s_client, temp_path, namespace=namespace)
            import os
            os.unlink(temp_path)
            return "Successfully applied YAML configuration to the cluster."
        except utils.FailToCreateError as failure:
            import os
            os.unlink(temp_path)
            # Sometimes create_from_yaml fails if resource already exists and needs update.
            # Let's try replacing or patching or just report the failure detail.
            reasons = []
            for exc in failure.api_exceptions:
                try:
                    err_body = json.loads(exc.body)
                    reasons.append(err_body.get("message", str(exc)))
                except:
                    reasons.append(str(exc))
            return f"Failed to apply some resources: {', '.join(reasons)}"
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

