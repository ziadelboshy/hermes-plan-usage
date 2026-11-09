"""Shared fixtures for the Plan Usage tests. Everything is offline: no network, no real credentials,
no real Hermes home. Hermes core modules the backend imports are replaced with small fakes."""

from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_api():
    spec = importlib.util.spec_from_file_location("plan_usage_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A fresh backend module whose settings live in tmp_path and whose caches start empty."""
    module = load_api()
    monkeypatch.setattr(module, "_config_path", lambda: tmp_path / "plugin-data" / "settings.json")
    monkeypatch.setattr(module, "_spawn", lambda tag, fn: fn())  # run background jobs inline
    return module


@pytest.fixture
def fake_module(monkeypatch):
    """Install a fake ``a.b.c`` module (and fake parents when the real ones are not importable)."""

    def install(dotted: str, **attrs):
        parts = dotted.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent in sys.modules:
                continue
            try:
                importlib.import_module(parent)
            except Exception:
                pkg = types.ModuleType(parent)
                pkg.__path__ = []  # mark as package
                monkeypatch.setitem(sys.modules, parent, pkg)
        mod = types.ModuleType(dotted)
        for k, v in attrs.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, dotted, mod)
        return mod

    return install


class FakeEntry(types.SimpleNamespace):
    """Stand-in for agent.credential_pool.PooledCredential. Carries a token so tests can prove the
    backend never returns or forwards it."""


class FakePool:
    """Stand-in for CredentialPool. ``move_entry`` renumbers priorities like core does; ``pinned``
    ids are re-sorted back to the front afterwards, like core's anthropic manual-first ordering."""

    def __init__(self, entries, pinned=()):
        self._entries = list(entries)
        self._renumber()
        self.pinned = set(pinned)
        self.moves = []
        self.move_result_none = False

    def _renumber(self):
        for p, e in enumerate(self._entries):
            e.priority = p

    def entries(self):
        return list(self._entries)

    def move_entry(self, entry_id, position):
        self.moves.append((entry_id, position))
        if self.move_result_none:
            return None
        target = next(e for e in self._entries if e.id == entry_id)
        rest = [e for e in self._entries if e.id != entry_id]
        rest.insert(position, target)
        rest.sort(key=lambda e: 0 if e.id in self.pinned else 1)  # stable: pinned rows stay first
        self._entries = rest
        self._renumber()
        return target


@pytest.fixture
def pools(fake_module):
    """Fake ``agent.credential_pool`` with per-provider pools; returns the provider -> FakePool map.

    ``state["factories"][provider]`` (a zero-arg callable) makes every ``load_pool`` call build a new
    pool, the way core re-seeds borrowed rows with fresh ids in a named profile."""
    state = {"pools": {}, "factories": {}, "loaded": [], "strategy": "fill_first", "raise": False}

    def load_pool(provider):
        if state["raise"]:
            raise RuntimeError("pool unreadable")
        if provider in state["factories"]:
            pool = state["factories"][provider]()
            state["loaded"].append(pool)
            return pool
        return state["pools"].setdefault(provider, FakePool([]))

    fake_module("agent.credential_pool", STATUS_DEAD="dead", STATUS_EXHAUSTED="exhausted",
                load_pool=load_pool, get_pool_strategy=lambda provider: state["strategy"])
    return state


def entry(eid, label, status=None, token="tok-SECRET-VALUE", source="manual", auth_type="oauth"):
    return FakeEntry(id=eid, label=label, last_status=status, priority=0, access_token=token,
                     runtime_api_key=token, source=source, auth_type=auth_type)


def fresh_id() -> str:
    return uuid.uuid4().hex[:6]
