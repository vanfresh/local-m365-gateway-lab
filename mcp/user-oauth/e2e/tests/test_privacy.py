"""The gateway log must not keep message text from an allowed read."""

import subprocess

from harness import E2E


def test_litellm_log_does_not_contain_message_text():
    completed = subprocess.run(
        ["docker", "compose", "-f", str(E2E / "compose.yaml"), "logs", "litellm-gateway"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "hello-user-a" not in completed.stdout
    assert "hello-user-a" not in completed.stderr
