"""
Secure isolation use case: an agent lets an LLM decide what code to run,
and that code might be malicious - by accident (a hallucinated action) or
on purpose (a manipulated/jailbroken prompt). This runs three real attacks
a compromised script might attempt, then runs a fourth check whose result
can only be seen by inspecting the cluster node directly - see
../README.md for how to do that and what it proves.
"""

from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

GENERATED_CODE = """
import os
import time

def attempt(label, fn):
    try:
        result = fn()
        print(f"[{label}] NOT BLOCKED: {result!r}")
    except Exception as e:
        print(f"[{label}] blocked: {type(e).__name__}: {e}")

attempt("read /etc/shadow", lambda: open("/etc/shadow").read())

attempt("setuid(0)", lambda: os.setuid(0))

def steal_sa_token():
    with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
        return f.read()
attempt("steal ServiceAccount token", steal_sa_token)

print("sandbox-isolation-probe-8f2e1c: running for 45s, check the node now")
time.sleep(45)
print("sandbox-isolation-probe-8f2e1c: done")
"""


def main() -> None:
    client = SandboxClient(connection_config=SandboxInClusterConnectionConfig())
    sandbox = client.create_sandbox(
        warmpool="python-sandbox-warmpool",
        namespace="default",
    )

    try:
        sandbox.files.write("attack_attempt.py", GENERATED_CODE)
        result = sandbox.commands.run("python3 attack_attempt.py", timeout=60)

        print(result.stdout)
        if result.stderr:
            print("--- stderr ---")
            print(result.stderr)
        print(f"exit_code={result.exit_code}")
    finally:
        sandbox.terminate()


if __name__ == "__main__":
    main()