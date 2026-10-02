import os
from pathlib import Path

from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig


ROUTER_URL = os.getenv("ROUTER_URL", "http://127.0.0.1:8080")
ROUTER_AUTH_TOKEN = os.getenv("ROUTER_AUTH_TOKEN")

UNTRUSTED_CODE_PATH = Path(__file__).parent / "untrusted-code.py"


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
            UNTRUSTED_CODE_PATH.read_text(),
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
