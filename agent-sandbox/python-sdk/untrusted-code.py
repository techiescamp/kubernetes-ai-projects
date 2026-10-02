import os
import subprocess
import sys


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
    lambda: read_one_byte("/var/run/secrets/kubernetes.io/serviceaccount/token"),
)


print("\n--- Host process visibility check ---")
print("Starting sandbox-isolation-probe-8f2e1c for 45 seconds")

probe_command = [
    sys.executable,
    "-c",
    "import time; time.sleep(45)",
    "sandbox-isolation-probe-8f2e1c",
]

probe = subprocess.Popen(probe_command)

print(f"Probe PID inside sandbox: {probe.pid}")
probe.wait(timeout=50)

print("Host process visibility check completed")
