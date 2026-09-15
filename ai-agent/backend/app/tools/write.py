import json
from typing import Any, Dict, Optional

from langchain_core.tools import tool
from kubernetes import client
from kubernetes.client.rest import ApiException

from .kube import logger, _api_error_message, _ALLOWED_KINDS, _ALLOWED_KINDS_CI
from .guards import check_write_allowed, _restart_would_not_help

@tool
def label_node(node_name: str, labels: Dict[str, Any]) -> str:
    """
    Add or update labels on a Node (equivalent to `kubectl label node <name> k=v --overwrite`).
    This is the fix when a pod is Pending because its nodeSelector/nodeAffinity matches no node -
    label a node so the scheduler can place it. Pass {"key": null} to REMOVE a label.
    Only labels are changed; nothing else about the node is touched, and this never cordons,
    drains or deletes anything.
    """
    try:
        v1 = client.CoreV1Api()
        v1.patch_node(name=node_name, body={"metadata": {"labels": labels}})
        node = v1.read_node(name=node_name)
        applied = {k: v for k, v in (node.metadata.labels or {}).items() if k in labels}
        return (
            f"Successfully labelled node '{node_name}'. Requested {json.dumps(labels)}; "
            f"node now reports {json.dumps(applied)}. Any pod Pending only because of a matching "
            f"nodeSelector should now schedule."
        )
    except Exception as e:
        return f"Error labelling node '{node_name}': {str(e)}"


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
        refusal = check_write_allowed("Pod", pod_name, namespace)
        if refusal:
            return refusal
        v1 = client.CoreV1Api()
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        pointless = _restart_would_not_help(pod)
        if pointless:
            return (
                f"REFUSED: restarting Pod/{pod_name} in '{namespace}' would change nothing - it is "
                f"failing because {pointless}. That fault is in the spec (or the cluster around "
                f"it), so the replacement pod fails identically. Patch the actual cause instead: "
                f"fix the image with update_pod_image/patch_deployment_image, the env/volumes/"
                f"selector with patch_deployment, or create the missing ConfigMap/Secret/node label."
            )
        if not (pod.metadata.owner_references or []):
            return (
                f"REFUSED: Pod/{pod_name} in '{namespace}' is a standalone pod (no "
                f"ownerReferences). 'Restarting' it means deleting it, and nothing would recreate "
                f"it - the pod would be gone for good. Fix the actual cause instead; if it must be "
                f"recreated, capture its spec with get_resource, then delete_resource with "
                f"recreating=true followed by apply_kubernetes_yaml."
            )
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
        refusal = check_write_allowed("Pod", pod_name, namespace)
        if refusal:
            return refusal
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
        refusal = check_write_allowed("Deployment", deployment_name, namespace)
        if refusal:
            return refusal
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
        refusal = check_write_allowed("Deployment", deployment_name, namespace)
        if refusal:
            return refusal
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
        refusal = check_write_allowed("Deployment", deployment_name, namespace)
        if refusal:
            return refusal
        try:
            dep = client.AppsV1Api().read_namespaced_deployment(name=deployment_name, namespace=namespace)
            selector = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
            if selector:
                for pod in client.CoreV1Api().list_namespaced_pod(
                        namespace=namespace, label_selector=selector).items:
                    pointless = _restart_would_not_help(pod)
                    if pointless:
                        return (
                            f"REFUSED: rolling Deployment/{deployment_name} in '{namespace}' would "
                            f"change nothing - its pods are failing because {pointless}. New pods "
                            f"come from the same template and fail the same way. Patch the template "
                            f"instead (patch_deployment_image for the image, patch_deployment for "
                            f"env/volumes/selector), or create the missing dependency."
                        )
        except ApiException:
            pass
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
        refusal = check_write_allowed("Deployment", deployment_name, namespace)
        if refusal:
            return refusal
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
                refusal = check_write_allowed(kind, name, doc_namespace, doc)
                if refusal:
                    results.append(f"{kind}/{name}: {refusal}")
                    continue
                if is_namespaced and not doc_namespace:
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
        refusal = check_write_allowed("Pod", pod_name, namespace)
        if refusal:
            return refusal
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
def delete_resource(kind: str, name: str, namespace: Optional[str] = None,
                    recreating: bool = False) -> str:
    """
    Delete a resource by kind and name. Set recreating=true ONLY when this delete is the first
    half of a delete-then-recreate you are about to complete with apply_kubernetes_yaml (needed
    to change an immutable field) - it is required before deleting a standalone Pod, because
    nothing recreates one and deleting it otherwise just destroys the workload.
    Restricted to the same allowed kinds as
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
    refusal = check_write_allowed(kind, name, namespace)
    if refusal:
        return refusal
    try:
        from kubernetes import dynamic
        from kubernetes.client import api_client

        dyn = dynamic.DynamicClient(api_client.ApiClient())
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
            resource = dyn.resources.get(kind=kind)
        is_namespaced = getattr(resource, "namespaced", True)
        if is_namespaced and not namespace:
            return (
                f"Error: no namespace given for namespaced kind '{kind}'. Specify the namespace "
                f"'{name}' actually lives in and retry - this tool will not default to 'default'."
            )

        if kind == "Node":
            return (
                f"REFUSED: deleting Node/{name} removes a machine from the cluster and evicts "
                f"everything on it. Node labels/taints can be changed with label_node or "
                f"apply_kubernetes_yaml; removing a node is an infrastructure operation for a human."
            )

        if kind == "Pod":
            try:
                pod = client.CoreV1Api().read_namespaced_pod(name=name, namespace=namespace)
                if not (pod.metadata.owner_references or []):
                    if not recreating:
                        return (
                            f"REFUSED: Pod/{name} in '{namespace}' is a standalone pod (no "
                            f"ownerReferences), so NOTHING will recreate it - deleting it destroys "
                            f"the workload permanently and does not fix anything. If the pod is "
                            f"broken, fix the underlying cause instead (the node label/selector, "
                            f"image, config or volume it is waiting on). If you genuinely must "
                            f"recreate it, first capture its full spec with get_resource, then "
                            f"call this tool again with recreating=true and immediately re-create "
                            f"it with apply_kubernetes_yaml."
                        )
                    logger.warning(
                        "deleting standalone pod %s/%s for a recreate - caller must re-create it",
                        namespace, name,
                    )
            except ApiException:
                pass

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
                pass

        if is_namespaced:
            resource.delete(name=name, namespace=namespace)
        else:
            resource.delete(name=name)

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
