import os
from typing import Optional

from kubernetes import client
from kubernetes.client.rest import ApiException

AGENT_NAMESPACE = os.getenv("AGENT_NAMESPACE", "ai-agent")
AGENT_SERVICE_ACCOUNT = os.getenv("AGENT_SERVICE_ACCOUNT", "ai-agent")
PROTECTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", AGENT_NAMESPACE}
_RBAC_KINDS = {"role", "clusterrole", "rolebinding", "clusterrolebinding"}
_BINDING_KINDS = {"rolebinding", "clusterrolebinding"}


def _targets_agent_identity(kind: str, name: str, namespace: Optional[str], body: Optional[dict]) -> bool:
    """
    True if this write would change the agent's OWN permissions.

    Blocking by object name alone is not enough: the obvious escalation is to create a BRAND NEW
    ClusterRoleBinding (any name at all) whose subject is this agent's ServiceAccount, bound to
    cluster-admin. So the subjects of any binding being written are inspected too.
    """
    k = (kind or "").lower()
    if k not in _RBAC_KINDS and k != "serviceaccount":
        return False

    if k == "serviceaccount" and name == AGENT_SERVICE_ACCOUNT and namespace == AGENT_NAMESPACE:
        return True
    if k in ("clusterrole", "clusterrolebinding") and name == AGENT_SERVICE_ACCOUNT:
        return True
    if k in ("role", "rolebinding") and namespace == AGENT_NAMESPACE:
        return True

    if k in _BINDING_KINDS and isinstance(body, dict):
        for subject in body.get("subjects") or []:
            if not isinstance(subject, dict):
                continue
            s_kind = (subject.get("kind") or "").lower()
            if s_kind == "serviceaccount" and subject.get("name") == AGENT_SERVICE_ACCOUNT \
                    and subject.get("namespace") == AGENT_NAMESPACE:
                return True
            if s_kind == "group" and AGENT_NAMESPACE in (subject.get("name") or ""):
                return True
    return False


def _target_is_unhealthy(kind: str, name: str, namespace: Optional[str]) -> Optional[bool]:
    """
    Is this object currently broken? None means "couldn't tell".

    Used to allow writes in protected namespaces only once something is actually failing there,
    which is what the owner asked for: hands off the system components until they break.
    """
    k = (kind or "").lower()
    try:
        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()
        if k == "pod":
            pod = v1.read_namespaced_pod(name=name, namespace=namespace)
            if (pod.status.phase or "") not in ("Running", "Succeeded"):
                return True
            for cs in (pod.status.container_statuses or []):
                if not cs.ready or (cs.restart_count or 0) > 3:
                    return True
            return False
        if k in ("deployment", "statefulset", "daemonset"):
            reader = {
                "deployment": apps.read_namespaced_deployment,
                "statefulset": apps.read_namespaced_stateful_set,
                "daemonset": apps.read_namespaced_daemon_set,
            }[k]
            obj = reader(name=name, namespace=namespace)
            status = obj.status
            if k == "daemonset":
                return (status.number_ready or 0) < (status.desired_number_scheduled or 0)
            desired = (obj.spec.replicas if obj.spec.replicas is not None else 1)
            return (status.ready_replicas or 0) < desired
    except ApiException as e:
        if e.status == 404:
            return True
        return None
    except Exception:
        return None
    return None


_SPEC_BAKED_REASONS = {
    "CreateContainerConfigError": "a referenced ConfigMap/Secret or key is missing or wrong",
    "ImagePullBackOff": "the image cannot be pulled (wrong name/tag or missing credentials)",
    "ErrImagePull": "the image cannot be pulled (wrong name/tag or missing credentials)",
    "InvalidImageName": "the image name is not valid",
    "CreateContainerError": "the container cannot be created from this spec",
    "RunContainerError": "the container cannot be started from this spec",
}


def _restart_would_not_help(pod) -> Optional[str]:
    """
    If the pod is failing for a reason a restart cannot possibly clear, return an explanation.

    Restarting is the classic non-fix: it looks like action, changes nothing, and the pod comes
    back in exactly the same state because the fault lives in the spec, not in the running
    container. Detect that case and say what to patch instead.
    """
    statuses = list(pod.status.container_statuses or []) + list(pod.status.init_container_statuses or [])
    for cs in statuses:
        waiting = getattr(cs.state, "waiting", None) if cs.state else None
        reason = getattr(waiting, "reason", None) if waiting else None
        if reason in _SPEC_BAKED_REASONS:
            return f"{reason} - {_SPEC_BAKED_REASONS[reason]}"
    if (pod.status.phase or "") == "Pending" and not (pod.spec.node_name or ""):
        for cond in (pod.status.conditions or []):
            if cond.type == "PodScheduled" and cond.status == "False":
                return (
                    f"the pod cannot be scheduled ({cond.reason or 'Unschedulable'}: "
                    f"{(cond.message or '')[:160]})"
                )
    return None


def check_write_allowed(kind: str, name: str, namespace: Optional[str] = None,
                        body: Optional[dict] = None) -> Optional[str]:
    """
    Gate every write. Returns a refusal message, or None when the write may proceed.

    Three rules, all requested explicitly by the cluster owner:
      1. Never modify the agent's own RBAC (it must not be able to grant itself more access).
      2. Never read Secret values (enforced in the read tools; Secrets remain writable).
      3. Don't touch system namespaces while they are healthy - only once something there is
         actually broken, which is exactly when the agent is supposed to help.
    """
    if _targets_agent_identity(kind, name, namespace, body):
        return (
            f"REFUSED: {kind}/{name} controls this agent's own permissions. The agent is not "
            f"allowed to modify its own RBAC (its ServiceAccount '{AGENT_SERVICE_ACCOUNT}', its "
            f"ClusterRole/ClusterRoleBinding, anything in the '{AGENT_NAMESPACE}' namespace, or any "
            f"binding that grants access to it) - that would let it escalate its own privileges. "
            f"This is permanent, not a transient error: a human must make this change directly. "
            f"RBAC for every OTHER workload is fully writable."
        )

    if namespace in PROTECTED_NAMESPACES:
        unhealthy = _target_is_unhealthy(kind, name, namespace)
        if unhealthy is False:
            return (
                f"REFUSED: {kind}/{name} is in the protected system namespace '{namespace}' and is "
                f"currently healthy. System components are left alone unless they are actually "
                f"broken. If you believe it IS broken, show the failing state first "
                f"(describe_pod/get_pod_events) - the guard allows the write once the object is "
                f"not Running/Ready or is missing."
            )
        if unhealthy is None:
            return (
                f"REFUSED: {kind}/{name} is in the protected system namespace '{namespace}' and its "
                f"health could not be determined, so the write is refused by default. Inspect it "
                f"first and only act on a confirmed failure."
            )
    return None

