import json
import logging
from typing import Any, Dict, Optional

from langchain_core.tools import tool
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

try:
    config.load_incluster_config()
except Exception:
    try:
        config.load_kube_config()
    except Exception as e:
        logger.error(f"Could not load any Kubernetes configuration (in-cluster or kubeconfig): {e}")

_ALLOWED_KINDS = {
    "Pod", "Deployment", "Service", "ConfigMap", "PersistentVolumeClaim", "PersistentVolume",
    "Ingress", "Job", "CronJob", "StatefulSet", "DaemonSet", "Namespace",
    "StorageClass", "ReplicaSet", "HorizontalPodAutoscaler", "PodDisruptionBudget",
    "NetworkPolicy", "ServiceAccount", "Endpoints", "ResourceQuota", "LimitRange",
    "PriorityClass", "VolumeAttachment", "CustomResourceDefinition", "ReplicationController",
    "Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding", "Secret",
    "Node",
}
_ALLOWED_KINDS_CI = {k.lower(): k for k in _ALLOWED_KINDS}

def _api_error_message(e) -> str:
    try:
        body = json.loads(e.body)
        msg = body.get("message")
        if msg:
            return f"{msg} (HTTP {e.status})"
    except Exception:
        pass
    return f"{getattr(e, 'reason', 'error')} (HTTP {getattr(e, 'status', '?')})"
