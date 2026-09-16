from . import kube

from .read import (
    check_permission, list_namespaces, get_pod_status, get_pod_logs, get_pod_events, describe_pod,
    get_cluster_events, list_nodes, describe_node, list_deployments, describe_deployment,
    list_replicasets, list_services, list_ingresses, list_configmaps, list_secrets, list_pvcs,
    list_jobs, list_cronjobs, list_statefulsets, list_daemonsets, list_hpas, get_resource_usage,
    get_resource,
)
from .write import (
    restart_pod, update_pod_image, patch_deployment_image, patch_deployment,
    rollout_restart_deployment, scale_deployment, apply_kubernetes_yaml, create_namespace,
    label_node, create_pod, delete_resource,
)

diag_tools = [
    list_namespaces, get_pod_status, get_pod_logs, get_pod_events, describe_pod,
    get_cluster_events, list_nodes, describe_node, list_deployments, describe_deployment,
    list_replicasets, list_services, list_ingresses, list_configmaps, list_secrets,
    list_pvcs, list_jobs, list_cronjobs, list_statefulsets, list_daemonsets, list_hpas,
    get_resource_usage, get_resource, check_permission,
]
remedy_tools = [
    restart_pod, update_pod_image, patch_deployment_image, patch_deployment,
    rollout_restart_deployment, scale_deployment, apply_kubernetes_yaml, create_namespace,
    label_node, create_pod, delete_resource,
]

diag_tool_map = {t.name: t for t in diag_tools}
remedy_tool_map = {t.name: t for t in remedy_tools}
