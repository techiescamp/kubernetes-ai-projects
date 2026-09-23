# kubernetes-ai-projects
List of projects to learn AI implementation on Kubernetes

## ai-agent
KubeCheck - a LangGraph agent that diagnoses and (with approval) remediates Kubernetes problems
via AWS Bedrock. See [`ai-agent/README.md`](ai-agent/README.md).

## agent-sandbox
Runs agent-generated code inside gVisor-isolated sandboxes
([kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)) instead of a
plain pod.

- [`agent-sandbox/ai-agent-sandbox/`](agent-sandbox/ai-agent-sandbox/README.md) - KubeCheck's
  backend deployed as a gVisor `Sandbox` via `SandboxTemplate`/`SandboxWarmPool`/`SandboxClaim`.
- `agent-sandbox/python-sdk/` - Python SDK example: claims a sandbox, runs a deliberately
  malicious script inside it, proves the isolation holds.

Run it (`untrusted-code.py` connects in-cluster, so it has to run as a pod, not locally -
`kubectl port-forward` doesn't work against gVisor pods here):

```sh
kubectl apply -f agent-sandbox/python-sdk/python-warmpool.yaml -f agent-sandbox/python-sdk/rbac.yaml
kubectl create configmap python-sdk-demo-script --from-file=untrusted-code.py=agent-sandbox/python-sdk/untrusted-code.py -n default
kubectl run python-sdk-demo --rm -it --restart=Never \
  --image=python:3.12-slim \
  --overrides='{"spec":{"serviceAccountName":"python-sdk-demo","containers":[{"name":"demo","image":"python:3.12-slim","command":["/bin/sh","-c","pip install --quiet k8s-agent-sandbox && python /app/untrusted-code.py"],"volumeMounts":[{"name":"script","mountPath":"/app"}]}],"volumes":[{"name":"script","configMap":{"name":"python-sdk-demo-script"}}]}}'
kubectl delete configmap python-sdk-demo-script -n default
```