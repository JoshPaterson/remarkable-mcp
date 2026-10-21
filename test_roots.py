"""The configured library root and conversions between path spaces."""

import pytest

from remarkable_mcp import resources, roots, tools


@pytest.mark.parametrize(
    "value, expected",
    [("", "/"), ("/", "/"), ("Work", "/Work"), ("/Work/", "/Work"), (" /Work ", "/Work")],
)
def test_get_root_normalizes(monkeypatch, value, expected):
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", value)
    assert roots.get_root() == expected


def test_no_root_leaves_paths_alone(monkeypatch):
    monkeypatch.delenv("REMARKABLE_ROOT_PATH", raising=False)
    assert roots.is_under_root("/Anything")
    assert roots.to_root_relative("/A/B") == "/A/B"
    assert roots.to_library_path("/A/B") == "/A/B"


def test_root_scoping_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
    assert roots.is_under_root("/work")
    assert roots.is_under_root("/WORK/Project")
    assert not roots.is_under_root("/Workshop")
    assert not roots.is_under_root("/Other/Work")


def test_conversions_round_trip(monkeypatch):
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
    assert roots.to_root_relative("/Work/Project/Notes") == "/Project/Notes"
    assert roots.to_root_relative("/work") == "/"
    assert roots.to_root_relative("/Other") == "/Other"
    assert roots.to_library_path("/Project/Notes") == "/Work/Project/Notes"
    assert roots.to_library_path("/") == "/Work"
    for path in ("/", "/Project", "/Project/Notes"):
        assert roots.to_root_relative(roots.to_library_path(path)) == path


def test_explicit_root_overrides_environment(monkeypatch):
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
    assert roots.is_under_root("/Home/x", "/Home")
    assert roots.to_root_relative("/Home/x", "/Home") == "/x"
    assert roots.to_library_path("/x", "/") == "/x"


def test_tools_and_resources_share_the_helpers():
    assert tools._get_root_path is resources._get_root_path is roots.get_root
    assert tools._is_within_root is resources._is_within_root is roots.is_under_root
    assert tools._apply_root_filter is resources._apply_root_filter is roots.to_root_relative
    assert tools._resolve_root_path is roots.to_library_path
