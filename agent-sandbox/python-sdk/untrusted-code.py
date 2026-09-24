import os

from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig


ROUTER_URL = os.getenv("ROUTER_URL", "http://127.0.0.1:8080")
ROUTER_AUTH_TOKEN = os.getenv("ROUTER_AUTH_TOKEN")


GENERATED_CODE = r"""
import os
import subprocess
import sys


TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
PROBE_NAME = "sandbox-isolation-probe-8f2e1c"


def attempt(label, function):
    try:
        function()
        print(f"[{label}] NOT BLOCKED")
    except Exception as error:
        print(f"[{label}] blocked: {type(error).__name__}: {error}")


def read_one_byte(path):
    with open(path, "rb") as file:
        file.read(1)


attempt(
    "read /etc/shadow",
    lambda: read_one_byte("/etc/shadow"),
)

attempt(
    "setuid(0)",
    lambda: os.setuid(0),
)

attempt(
    "read ServiceAccount token",
    lambda: read_one_byte(TOKEN_PATH),
)


print("\n--- Host process visibility check ---")
print(f"Starting {PROBE_NAME} for 45 seconds")

probe_command = [
    sys.executable,
    "-c",
    "import time; time.sleep(45)",
    PROBE_NAME,
]

probe = subprocess.Popen(probe_command)

print(f"Probe PID inside sandbox: {probe.pid}")
probe.wait(timeout=50)

print("Host process visibility check completed")
"""


def main():
    config = SandboxDirectConnectionConfig(api_url=ROUTER_URL)
    client = SandboxClient(connection_config=config)

    print("Claiming a sandbox from python-sandbox-warmpool...")

    sandbox = client.create_sandbox(
        warmpool="python-sandbox-warmpool",
        namespace="default",
    )

    try:
        if ROUTER_AUTH_TOKEN:
            sandbox.connector.session.headers.update({
                "Authorization": f"Bearer {ROUTER_AUTH_TOKEN}"
            })

        pod_name = sandbox.get_pod_name()

        print(f"Claim:   {sandbox.claim_name}")
        print(f"Sandbox: {sandbox.sandbox_id}")
        print(f"Pod:     {pod_name}")

        sandbox.files.write(
            "attack_attempt.py",
            GENERATED_CODE,
        )

        print("\nStarting the 45-second isolation test.")
        print(
            "On the node, run:\n"
            "sudo ps -eo pid,args | "
            "grep '[s]andbox-isolation-probe-8f2e1c'",
            flush=True,
        )

        result = sandbox.commands.run(
            "python3 attack_attempt.py",
            timeout=90,
        )

        print("\n--- stdout ---")
        print(result.stdout)

        if result.stderr:
            print("--- stderr ---")
            print(result.stderr)

        print(f"exit_code={result.exit_code}")

    finally:
        print("Terminating the sandbox claim...")
        sandbox.terminate()


if __name__ == "__main__":
    main()