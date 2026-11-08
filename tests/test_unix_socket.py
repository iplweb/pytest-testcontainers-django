"""``postgres_connect`` = socket/auto: decision logic and socket probing."""

from __future__ import annotations

import os
import socket
import stat
import sys
import tempfile

import pytest

from pytest_testcontainers_django.containers import (
    _make_socket_dir,
    _socket_reachable,
    _wants_socket,
)


def test_port_mode_never_uses_socket() -> None:
    assert _wants_socket("port", reuse_name=None) is False


def test_reuse_mode_stays_on_tcp() -> None:
    assert _wants_socket("auto", reuse_name="proj-tc-psql-x") is False
    assert _wants_socket("socket", reuse_name="proj-tc-psql-x") is False


def test_non_linux_stays_on_tcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _wants_socket("auto", reuse_name=None) is False


def test_linux_ephemeral_tries_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _wants_socket("auto", reuse_name=None) is True
    assert _wants_socket("socket", reuse_name=None) is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/tmp + AF_UNIX")
def test_socket_dir_is_short_and_world_writable() -> None:
    path = _make_socket_dir()
    try:
        assert path.startswith("/tmp/")
        # AF_UNIX limit (~107 bytes) must leave room for ".s.PGSQL.<port>".
        assert len(os.path.join(path, ".s.PGSQL.65535")) < 100
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o777
    finally:
        os.rmdir(path)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX")
def test_socket_reachable() -> None:
    path = tempfile.mkdtemp(prefix="tcpgt-", dir="/tmp")
    sock_path = os.path.join(path, ".s.PGSQL.5432")
    try:
        assert _socket_reachable(path, 5432) is False  # no file yet
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        try:
            assert _socket_reachable(path, 5432) is True
            assert _socket_reachable(path, 5433) is False  # other port, no file
        finally:
            server.close()
        # Stale file left behind with nobody listening.
        assert _socket_reachable(path, 5432) is False
    finally:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        os.rmdir(path)
