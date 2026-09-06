from __future__ import annotations

import pytest

from agent_port.infrastructure.repositories import _canonical_remote


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/owner/project.git",
        "https://user:synthetic-token@GitHub.com:443/owner/project.git?token=secret",
        "git@github.com:owner/project.git",
        "ssh://git@github.com:22/owner/project.git",
        "ssh://another-user@github.com/owner/project.git",
    ],
)
def test_remote_identity_ignores_transport_and_credentials(remote: str) -> None:
    assert _canonical_remote(remote) == "github.com/owner/project"


@pytest.mark.parametrize(
    "remote",
    [
        "",
        "../project.git",
        "/srv/project.git",
        "C:\\repos\\project.git",
        "file:///srv/project.git",
        "https://[broken/owner/project",
        "https://host:invalid/owner/project",
        "https://host/",
    ],
)
def test_nonportable_or_invalid_remote_does_not_produce_a_mapping(remote: str) -> None:
    assert _canonical_remote(remote) is None


def test_distinct_git_server_ports_are_not_the_same_repository() -> None:
    assert _canonical_remote("ssh://git@host:2222/owner/project.git") != _canonical_remote(
        "ssh://git@host:2223/owner/project.git"
    )
