"""Host-side pytest session for the lab containers."""

import pytest

from harness import wait_ready


@pytest.fixture(scope="session", autouse=True)
def lab_ready():
    wait_ready()
