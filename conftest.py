"""
Pytest configuration for remarkable-mcp tests.

Adds --run-integration flag for live SSH tests against a connected tablet.
"""

import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolate_blob_cache(monkeypatch):
    """Point the cloud blob cache at a fresh temp dir for every test.

    The cloud client caches content-addressed blobs on disk. Without isolation,
    tests that mock HTTP would write blobs into the developer's real
    ~/.remarkable cache and then read them back on reruns, bypassing the mock
    and making call-count assertions nondeterministic. A per-test temp dir keeps
    each test hermetic and avoids polluting the real cache.
    """
    with tempfile.TemporaryDirectory() as cache_dir:
        monkeypatch.setenv("REMARKABLE_CACHE_DIR", cache_dir)
        try:
            from remarkable_mcp import api

            api.reset_client_cache()
        except Exception:
            pass
        yield


@pytest.fixture(autouse=True)
def restore_tool_registry():
    """Restore the server's registered tools after every test.

    Tests register write tools in different transport modes and remove them
    again; without restoring the registry, a later test can find a tool
    missing or registered for another mode, and pass or fail depending on the
    order the tests run in.
    """
    from remarkable_mcp.server import mcp

    tools = mcp._tool_manager._tools
    saved = dict(tools)
    try:
        yield
    finally:
        tools.clear()
        tools.update(saved)


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests against a connected reMarkable tablet via SSH",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: tests requiring a connected reMarkable tablet")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip_integration = pytest.mark.skip(reason="need --run-integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
