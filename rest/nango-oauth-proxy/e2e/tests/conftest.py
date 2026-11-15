import os
import time
import urllib.request

import pytest

GATEWAY = os.environ.get("GATEWAY", "http://127.0.0.1:4400")
KEYCLOAK = os.environ.get("KEYCLOAK", "http://127.0.0.1:8085")


def _ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status < 500
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def lab_ready():
    deadline = time.time() + 240
    while time.time() < deadline:
        if _ok(GATEWAY + "/health") and _ok(KEYCLOAK + "/realms/master"):
            return
        time.sleep(2)
    raise RuntimeError("lab not ready")
