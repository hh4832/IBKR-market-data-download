"""Fail closed if an offline test attempts an outbound connection."""
import socket
import sys
import types
import pytest


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair

    def event_loop_socketpair(*args, **kwargs):
        # Windows implements asyncio's internal wakeup pipe with a temporary
        # loopback socket pair. Permit only its standard-library construction.
        with monkeypatch.context() as context:
            context.setattr(socket.socket, 'connect', original_connect)
            return original_socketpair(*args, **kwargs)

    def denied(*args, **kwargs):
        raise AssertionError('Network connections are forbidden in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'socketpair', event_loop_socketpair)


@pytest.fixture(autouse=True)
def offline_progress_import(monkeypatch):
    # The recovered orchestrator imports tqdm unconditionally, even when all
    # integration tests disable progress. Keep CI's minimal offline dependencies.
    def disabled_progress(*args, **kwargs):
        raise AssertionError('Offline orchestration fixtures must disable progress')
    progress = types.ModuleType('tqdm.auto')
    progress.tqdm = disabled_progress
    monkeypatch.setitem(sys.modules, 'tqdm.auto', progress)
