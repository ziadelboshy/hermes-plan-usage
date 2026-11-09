"""Offline tests for the Plan Usage backend (dashboard/plugin_api.py).

No network, no real credentials, no real Hermes home: Hermes core modules are faked (conftest.py)
and the plugin's settings live in pytest's tmp_path.
"""

from __future__ import annotations

import builtins
import io
import json
import os
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from conftest import entry

SECRET = "sk-ant-admin01-SECRET-VALUE"


# ── config ───────────────────────────────────────────────────────────────────

def test_config_defaults(api):
    cfg = api.load_config()  # nothing on disk yet
    assert cfg == {"sections": {"nous": True, "codex": True, "claude": True, "api": True},
                   "names": {}, "key_names": {}, "disabled": [], "poll_seconds": 120}
    assert api.normalize_config("garbage") == cfg
    assert api.normalize_config(None) == cfg


def test_normalize_config_validation(api):
    cfg = api.normalize_config({
        "sections": {"api": False, "bogus": True},
        "names": {"anthropic:abc": "  Work\x07 Max  ", "openai-codex:x": "", "bad key!": "x"},
        "key_names": {"anthropic_admin_key": "Org", "MY_CUSTOM_KEY": "dropped"},
        "admin_keys": [{"env": "MY_CUSTOM_KEY", "provider": "openai"}],  # custom slots: not supported, dropped
        "disabled": ["anthropic:abc", "OPENAI_ADMIN_KEY", "anthropic:abc", "bad key!", "", None],
        "poll_seconds": 5,
        "unknown": 1,
    })
    assert cfg["sections"] == {"nous": True, "codex": True, "claude": True, "api": False}
    assert cfg["names"] == {"anthropic:abc": "Work Max"}
    assert cfg["key_names"] == {"ANTHROPIC_ADMIN_KEY": "Org"}
    assert cfg["disabled"] == ["anthropic:abc", "OPENAI_ADMIN_KEY"]
    assert cfg["poll_seconds"] == 60
    assert "admin_keys" not in cfg and "unknown" not in cfg
    assert api.normalize_config({"poll_seconds": 99999})["poll_seconds"] == 1800
    assert api.normalize_config({"poll_seconds": "x"})["poll_seconds"] == 120
    assert api.normalize_config({"names": {"anthropic:a": "x" * 99}})["names"]["anthropic:a"] == "x" * 40


def test_save_and_load_roundtrip_in_plugin_storage(api, tmp_path):
    api.save_config({"sections": {"nous": False}, "names": {"anthropic:1": "Personal"}})
    stored = tmp_path / "plugin-data" / "settings.json"
    assert stored.is_file()
    assert json.loads(stored.read_text("utf-8"))["names"] == {"anthropic:1": "Personal"}
    cfg = api.load_config()
    assert cfg["sections"]["nous"] is False and cfg["names"] == {"anthropic:1": "Personal"}
    assert not (tmp_path / "plugin-data" / "settings.json.tmp").exists()


def test_names_and_disabled_are_capped(api):
    many = {f"anthropic:k{i}": f"Name {i}" for i in range(300)}
    cfg = api.normalize_config({"names": many, "disabled": [f"anthropic:k{i}" for i in range(300)]})
    assert len(cfg["names"]) == api.MAX_ITEMS == 200
    assert len(cfg["disabled"]) == 200
    long_key = "anthropic:" + "x" * 80
    cfg = api.normalize_config({"names": {long_key: "Too long"}, "disabled": [long_key]})
    assert cfg["names"] == {} and cfg["disabled"] == []
    assert len(api.normalize_config({"names": {"anthropic:a": "y" * 99}})["names"]["anthropic:a"]) == 40


def test_corrupt_settings_fall_back_to_defaults(api, tmp_path):
    path = tmp_path / "plugin-data" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", "utf-8")
    assert api.load_config() == api.normalize_config({})


def test_storage_unavailable_uses_defaults(api, monkeypatch):
    def boom():
        raise ImportError("plugins.plugin_storage missing")

    monkeypatch.setattr(api, "_config_path", boom)
    assert api.load_config() == api.normalize_config({})


def test_post_config_sanitizes_body(api):
    out = api.post_config({"config": {"poll_seconds": 1, "names": {"anthropic:a": "A"}}})
    assert out["ok"] and out["config"]["poll_seconds"] == 60
    assert api.load_config()["names"] == {"anthropic:a": "A"}
    assert api.post_config("garbage")["config"] == api.normalize_config({})


# ── secrets: only through agent.secret_scope.get_secret ─────────────────────

def test_secret_read_goes_through_get_secret(api, fake_module):
    asked = []
    fake_module("agent.secret_scope", get_secret=lambda name, default=None: asked.append(name) or f" {SECRET} ")
    assert api._secret("ANTHROPIC_ADMIN_KEY") == SECRET
    assert asked == ["ANTHROPIC_ADMIN_KEY"]


def test_secret_refuses_undeclared_names(api, fake_module):
    asked = []
    fake_module("agent.secret_scope", get_secret=lambda name, default=None: asked.append(name) or "x")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_ADMIN_KEY_WORK", "OPENAI_API_KEY", "HERMES_TOKEN"):
        with pytest.raises(ValueError):
            api._secret(name)
    assert asked == []


def test_secret_never_falls_back_to_os_environ(api, fake_module, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", SECRET)
    monkeypatch.setenv("OPENAI_ADMIN_KEY", SECRET)
    fake_module("agent.secret_scope", get_secret=lambda name, default=None: None)
    assert api._secret("ANTHROPIC_ADMIN_KEY") == ""
    slots = api.admin_key_slots(api.normalize_config({}))
    assert [s["present"] for s in slots] == [False, False]


def test_secret_scope_error_means_not_set(api, fake_module):
    def unscoped(name, default=None):
        raise RuntimeError("UnscopedSecretError: no profile scope while multiplexing")

    fake_module("agent.secret_scope", get_secret=unscoped)
    assert api._secret("OPENAI_ADMIN_KEY") == ""


def test_admin_key_slots_are_fixed_and_never_carry_values(api, fake_module):
    values = {"ANTHROPIC_ADMIN_KEY": SECRET}
    fake_module("agent.secret_scope", get_secret=lambda name, default=None: values.get(name))
    cfg = api.normalize_config({"key_names": {"ANTHROPIC_ADMIN_KEY": "Org"}, "disabled": ["OPENAI_ADMIN_KEY"]})
    slots = api.admin_key_slots(cfg)
    assert [(s["env"], s["provider"], s["present"], s["enabled"], s["name"]) for s in slots] == [
        ("ANTHROPIC_ADMIN_KEY", "anthropic", True, True, "Org"),
        ("OPENAI_ADMIN_KEY", "openai", False, False, "OpenAI API"),
    ]
    assert SECRET not in json.dumps(slots)


# ── the no-.env / no-auth.json rule ──────────────────────────────────────────

def _forbidden(path) -> bool:
    name = os.path.basename(os.fspath(path)) if isinstance(path, (str, bytes, os.PathLike)) else ""
    if isinstance(name, bytes):
        name = name.decode(errors="ignore")
    return name == ".env" or name.endswith(".env") or name == "auth.json" or "auth.json" in name


def test_backend_never_opens_env_or_auth_json(api, fake_module, pools, monkeypatch, tmp_path):
    """Full status + config + refresh with every section on: record every file the backend opens."""
    # A real-looking Hermes home next to the plugin storage: nothing in it may be touched.
    (tmp_path / ".env").write_text(f"ANTHROPIC_ADMIN_KEY={SECRET}\n", "utf-8")
    (tmp_path / "auth.json").write_text("{}", "utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    opened = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def spy_open(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    def spy_io_open(file, *a, **k):
        opened.append(str(file))
        return real_io_open(file, *a, **k)

    def spy_os_open(path, *a, **k):
        opened.append(str(path))
        return real_os_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_io_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    fake_module("agent.secret_scope", get_secret=lambda name, default=None: SECRET)
    fake_module("agent.account_usage", build_credits_view=lambda **k: SimpleNamespace(
        logged_in=True, balance_lines=("Total usable: $5.00",), depleted=False))
    monkeypatch.setattr(api, "_http_json", lambda url, headers: {"data": [], "has_more": False})
    pools["pools"]["anthropic"] = __import__("conftest").FakePool([entry("a", "Work")])

    api.save_config({"names": {"anthropic:a": "Work"}})
    status = api.post_refresh()
    api.get_status()
    api.get_config()
    assert status["api"]["keys"] and status["nous"] is not None
    assert not [p for p in opened if _forbidden(p)], opened
    assert SECRET not in json.dumps(status)
    assert "tok-SECRET-VALUE" not in json.dumps(status) + json.dumps(api.get_config())


def test_backend_code_has_no_env_reads_or_token_access():
    """Belt and braces for reviewers, on the parsed code (docstrings don't count): no os.environ /
    os.getenv, no .env parser, no token attributes, no private core modules."""
    import ast

    from conftest import ROOT

    tree = ast.parse((ROOT / "dashboard" / "plugin_api.py").read_text("utf-8"))
    attrs = {ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    attr_names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported |= {n.module or ""} | {f"{n.module}.{a.name}" for a in n.names}
    assert not {"os.environ", "os.getenv", "os.environ.get"} & attrs
    assert not {"runtime_api_key", "access_token", "refresh_token"} & attr_names
    forbidden = ("dotenv", "agent.account_usage_cache", "hermes_cli.auth_commands", "hermes_cli.auth_codex",
                 "hermes_cli.config.remove_env_value", "agent.account_usage._fetch_portal_account",
                 "agent.credential_pool._exhausted_until")
    assert not [m for m in imported if m.startswith(forbidden)], imported
    assert not [m for m in imported if m.rsplit(".", 1)[-1].startswith("_") and m != "__future__"], imported


# ── Claude / Codex pool metadata ─────────────────────────────────────────────

def test_default_account_name(api):
    assert api.default_account_name("anthropic", "anthropic-oauth-1", 1) == "Claude 1"
    assert api.default_account_name("anthropic", "claude_code", 3) == "Claude 3"
    assert api.default_account_name("openai-codex", "device_code", 2) == "ChatGPT 2"
    assert api.default_account_name("anthropic", "Work", 1) == "Work"
    assert api.default_account_name("anthropic", "", 4) == "Claude 4"


def test_pool_accounts_rows_names_default_and_no_tokens(api, pools):
    from conftest import FakePool

    pools["pools"]["anthropic"] = FakePool([entry("d", "Old", status="dead"), entry("a", "claude_code"),
                                            entry("b", "Work")])
    out = api.pool_accounts("anthropic", {"anthropic:b": "Side job"}, {"anthropic:d"})
    assert out["ok"] and out["strategy"] == "fill_first"
    rows = {r["key"]: r for r in out["accounts"]}
    assert [r["key"] for r in out["accounts"]] == ["anthropic:d", "anthropic:a", "anthropic:b"]  # pool order kept
    assert rows["anthropic:d"]["dead"] and not rows["anthropic:d"]["enabled"] and not rows["anthropic:d"]["is_default"]
    assert rows["anthropic:a"]["is_default"] and rows["anthropic:a"]["name"] == "Claude 2"
    assert rows["anthropic:b"]["name"] == "Side job" and rows["anthropic:b"]["custom_name"]
    assert rows["anthropic:b"]["pool_label"] == "Work"
    assert "tok-SECRET-VALUE" not in json.dumps(out)


def test_pool_accounts_empty_and_error_paths(api, pools):
    out = api.pool_accounts("openai-codex", {})
    assert out["ok"] and out["accounts"] == [] and "hermes auth add openai-codex" in out["hint"]
    pools["raise"] = True
    out = api.pool_accounts("anthropic", {})
    assert out == {"ok": False, "error": "could not read Hermes credential pool", "accounts": []}


def test_pool_accounts_without_hermes_api(api, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "agent.credential_pool", None)  # import -> ImportError
    out = api.pool_accounts("anthropic", {})
    assert out["ok"] is False and "unavailable" in out["error"] and out["accounts"] == []


def test_set_default_moves_entry_to_top(api, pools):
    from conftest import FakePool

    pool = FakePool([entry("x", "Main"), entry("y", "Work")])
    pools["pools"]["openai-codex"] = pool
    assert api.set_default_account("openai-codex:y") == {"ok": True}
    assert pool.moves == [("y", 0)] and [e.id for e in pool.entries()] == ["y", "x"]
    pools["strategy"] = "round_robin"
    assert "round_robin" in api.set_default_account("openai-codex:x")["note"]


def test_set_default_error_paths(api, pools):
    from conftest import FakePool

    pools["pools"]["anthropic"] = pool = FakePool([entry("a", "A")])
    for bad in ("nous:zz", "anthropic:", "", "gemini:a"):
        with pytest.raises(ValueError):
            api.set_default_account(bad)
    with pytest.raises(ValueError):
        api.set_default_account("anthropic:missing")
    pool.move_result_none = True
    assert api.post_default({"key": "anthropic:a"}) == {"ok": False, "error": "Hermes could not reorder that account"}
    assert api.post_default("garbage") == {"ok": False, "error": "unknown account"}
    pools["raise"] = True
    assert api.post_default({"key": "anthropic:a"})["ok"] is False


def _named_profile_pool():
    """What load_pool returns in a named profile: seeded rows get a new id on every load."""
    from conftest import FakePool, fresh_id

    return FakePool([
        entry("man1", "Work", source="manual"),
        entry(fresh_id(), "me@example.com", source="claude_code"),
        entry(fresh_id(), "hermes_pkce", source="hermes_pkce"),
        entry(fresh_id(), "ANTHROPIC_API_KEY", source="env:ANTHROPIC_API_KEY", auth_type="api_key"),
    ])


def test_seeded_accounts_keep_their_key_when_ids_change(api, pools):
    pools["factories"]["anthropic"] = _named_profile_pool
    first = api.pool_accounts("anthropic", {}, set())["accounts"]
    second = api.pool_accounts("anthropic", {"anthropic:claude_code": "Personal"},
                               {"anthropic:hermes_pkce"})["accounts"]
    keys = ["anthropic:man1", "anthropic:claude_code", "anthropic:hermes_pkce", "anthropic:env:ANTHROPIC_API_KEY"]
    assert [r["key"] for r in first] == [r["key"] for r in second] == keys
    by_key = {r["key"]: r for r in second}
    assert by_key["anthropic:claude_code"]["name"] == "Personal"  # the name survives a reload
    assert by_key["anthropic:hermes_pkce"]["enabled"] is False    # so does the switch
    assert by_key["anthropic:env:ANTHROPIC_API_KEY"]["api_key"] is True
    assert by_key["anthropic:claude_code"]["api_key"] is False
    assert api.normalize_config({"names": {k: "x" for k in keys}})["names"].keys() == set(keys)


def test_duplicate_seeded_source_falls_back_to_entry_id(api, pools):
    from conftest import FakePool

    pools["pools"]["anthropic"] = FakePool([entry("s1", "a", source="claude_code"),
                                            entry("s2", "b", source="claude_code")])
    assert [r["key"] for r in api.pool_accounts("anthropic", {})["accounts"]] == [
        "anthropic:claude_code", "anthropic:s2"]


def test_star_resolves_a_seeded_key_to_the_current_entry_id(api, pools):
    pools["factories"]["anthropic"] = _named_profile_pool
    assert api.set_default_account("anthropic:claude_code") == {"ok": True}
    pool = pools["loaded"][-1]
    moved = next(e for e in pool.entries() if e.source == "claude_code")
    assert pool.moves == [(moved.id, 0)] and moved.priority == 0
    with pytest.raises(ValueError):
        api.set_default_account("anthropic:codex_cli")  # no such source in this pool


def test_star_reports_failure_when_hermes_resorts_the_move_away(api, pools):
    from conftest import FakePool

    # Core keeps manually added anthropic credentials ahead of seeded ones.
    pools["pools"]["anthropic"] = pool = FakePool(
        [entry("m1", "Work", source="manual"), entry("s1", "me@example.com", source="claude_code")], pinned={"m1"})
    out = api.set_default_account("anthropic:claude_code")
    assert out["ok"] is False and pool.moves == [("s1", 0)]
    assert "manually added" in out["note"] and "#2" in out["note"]
    assert api.post_default({"key": "anthropic:claude_code"})["ok"] is False


# ── background threads ───────────────────────────────────────────────────────

def test_spawn_carries_the_callers_context_into_the_worker():
    """Hermes scopes secrets / HERMES_HOME with contextvars; the fetch thread must see the caller's."""
    import contextvars
    import threading

    from conftest import load_api

    module = load_api()  # the real _spawn, not the inline stand-in
    scope = contextvars.ContextVar("scope", default="unscoped")
    seen, done = [], threading.Event()

    def job():
        seen.append(scope.get())
        done.set()

    token = scope.set("profile-a")
    try:
        module._spawn("ctx-test", job)
    finally:
        scope.reset(token)
    assert done.wait(5)
    assert seen == ["profile-a"]


# ── forced refresh floor ─────────────────────────────────────────────────────

def test_forced_refresh_is_throttled_to_once_per_30_seconds(api, fake_module, monkeypatch):
    calls = []
    fake_module("agent.account_usage", build_credits_view=lambda **k: calls.append(1) or SimpleNamespace(
        logged_in=True, balance_lines=("Total usable: $5.00",), depleted=False))
    monkeypatch.setattr(api, "pool_accounts", lambda *a, **k: {"ok": True, "accounts": []})
    api.save_config({"sections": {"api": False}})
    clock = [1_000_000.0]
    monkeypatch.setattr(api.time, "time", lambda: clock[0])
    api.post_refresh()
    api.post_refresh()  # a second click / palette run right away: served from cache
    assert len(calls) == 1
    clock[0] += api.FORCE_FLOOR_S + 1
    api.post_refresh()
    assert len(calls) == 2


# ── section toggles ──────────────────────────────────────────────────────────

def test_sections_switched_off_are_not_built(api, fake_module, monkeypatch):
    called = []
    monkeypatch.setattr(api, "pool_accounts", lambda *a, **k: called.append("pool") or {"ok": True, "accounts": []})
    monkeypatch.setattr(api, "_nous_state", lambda force: called.append("nous") or {"ok": True})
    monkeypatch.setattr(api, "_api_state", lambda cfg, force: called.append("api") or {"ok": True, "keys": []})
    api.save_config({"sections": {"claude": False, "codex": False, "nous": False, "api": False}})
    out = api.build_status()
    assert called == []
    assert not {"claude", "codex", "nous", "api"} & set(out)
    api.save_config({"sections": {"claude": True, "codex": False, "nous": True, "api": False}})
    out = api.build_status()
    assert called == ["pool", "nous"] and {"claude", "nous"} <= set(out) and "codex" not in out


# ── Nous Portal ──────────────────────────────────────────────────────────────

def test_parse_nous_lines(api):
    lines = ["Subscription credits: $0.00", "Top-up credits: $9.91", "Total usable: $9.91"]
    assert api.parse_nous_lines(lines) == {"subscription_usd": 0.0, "topup_usd": 9.91, "total_usd": 9.91}
    assert api.parse_nous_lines(["Subscription credits: $1,200.50", "Top-up credits: $2"])["total_usd"] == 1202.5
    assert api.parse_nous_lines([])["total_usd"] is None


def test_fetch_nous_uses_hermes_credits_view(api, fake_module):
    seen = {}

    def view(**kw):
        seen.update(kw)
        return SimpleNamespace(logged_in=True, depleted=True, balance_lines=(
            "Plan: Plus", "Subscription credits: $1.50", "Top-up credits: $2.00", "Top up: https://portal",
            "(or run /topup)", ""))

    fake_module("agent.account_usage", build_credits_view=view)
    out = api._fetch_nous()
    assert seen == {"markdown": False, "timeout": api.HTTP_TIMEOUT_S}
    assert out["ok"] and out["depleted"] and out["total_usd"] == 3.5
    assert out["lines"] == ["Plan: Plus", "Subscription credits: $1.50", "Top-up credits: $2.00"]


def test_nous_not_signed_in_and_failure(api, fake_module):
    fake_module("agent.account_usage", build_credits_view=lambda **k: SimpleNamespace(logged_in=False))
    assert api._fetch_nous()["ok"] is False
    api._nous_cache.clear()
    assert api._nous_state(force=False) == {"ok": None, "loading": True}  # first poll: job runs, nothing cached yet

    def broken(**k):
        raise TimeoutError("portal hung")

    fake_module("agent.account_usage", build_credits_view=broken)
    api._nous_state(force=True)
    out = api._nous_state(force=False)
    assert out["ok"] is False and "TimeoutError" in out["error"] and "fetched_at" in out


# ── API spend ────────────────────────────────────────────────────────────────

def test_cost_sums(api):
    anth = [{"data": [{"results": [{"amount": "1234.5", "currency": "USD"}]},
                      {"results": [{"amount": "100"}, {"amount": None}, {"amount": "bad"}]}]}]
    assert api.sum_anthropic_cost(anth) == 13.35  # cents -> dollars
    oai = [{"data": [{"results": [{"amount": {"value": 0.06, "currency": "usd"}}]},
                     {"results": [{"amount": {"value": 1.5}}, {"amount": "weird"}]}]}]
    assert api.sum_openai_cost(oai) == 1.56


def test_fetch_api_spend_paginates(api, monkeypatch):
    calls = []

    def fake_http(url, headers):
        calls.append((url, headers))
        if len(calls) == 1:
            return {"data": [{"results": [{"amount": {"value": 2.0}}]}], "has_more": True, "next_page": "p2"}
        return {"data": [{"results": [{"amount": {"value": 3.25}}]}], "has_more": False}

    monkeypatch.setattr(api, "_http_json", fake_http)
    out = api._fetch_api_spend("openai", SECRET)
    assert out["ok"] and out["month_to_date_usd"] == 5.25
    assert calls[0][0].startswith("https://api.openai.com/v1/organization/costs?")
    assert "page=p2" in calls[1][0]
    assert calls[0][1]["Authorization"] == f"Bearer {SECRET}"


def test_anthropic_request_shape(api, monkeypatch):
    seen = {}

    def fake_http(url, headers):
        seen["url"], seen["headers"] = url, headers
        return {"data": [], "has_more": False}

    monkeypatch.setattr(api, "_http_json", fake_http)
    assert api._fetch_api_spend("anthropic", "k")["month_to_date_usd"] == 0.0
    assert seen["url"].startswith("https://api.anthropic.com/v1/organizations/cost_report?")
    assert "bucket_width=1d" in seen["url"] and "limit=31" in seen["url"]
    assert seen["headers"] == {"x-api-key": "k", "anthropic-version": "2023-06-01"}


def test_fetch_api_spend_error_paths_never_echo_secrets(api, monkeypatch):
    def denied(url, headers):
        raise HTTPError(url, 403, "Forbidden", {}, BytesIO(f'{{"error":"key {SECRET} bad"}}'.encode()))

    monkeypatch.setattr(api, "_http_json", denied)
    assert api._fetch_api_spend("anthropic", SECRET) == {"ok": False, "error": "HTTP 403: not an Admin key / no org access"}

    def offline(url, headers):
        raise URLError("no route")

    monkeypatch.setattr(api, "_http_json", offline)
    assert api._fetch_api_spend("openai", SECRET) == {"ok": False, "error": "network error (URLError)"}

    def garbage(url, headers):
        raise ValueError(f"bad json containing {SECRET}")

    monkeypatch.setattr(api, "_http_json", garbage)
    out = api._fetch_api_spend("openai", SECRET)
    assert out == {"ok": False, "error": "unexpected response (ValueError)"}


def test_empty_key_makes_no_request(api, monkeypatch):
    monkeypatch.setattr(api, "_http_json", lambda *a: pytest.fail("no request without a key"))
    assert api._fetch_api_spend("anthropic", "") == {"ok": False, "error": "key not set"}


def test_admin_key_is_never_forwarded_on_a_redirect(api):
    """A local server answers 302 to another path; the client must stop there, so the key header
    never reaches the redirect target. Loopback only, no external network."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    landed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/start"):
                self.send_response(302)
                self.send_header("Location", "/landing")
                self.end_headers()
                return
            landed.append(dict(self.headers))
            body = b'{"data": [], "has_more": false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert api._http_json(f"{url}/landing", {"x-api-key": "k"}) == {"data": [], "has_more": False}
        landed.clear()
        with pytest.raises(HTTPError) as err:
            api._http_json(f"{url}/start", {"x-api-key": SECRET, "Authorization": f"Bearer {SECRET}"})
        assert err.value.code == 302
        assert landed == []
    finally:
        server.shutdown()
        server.server_close()


def test_redirect_maps_to_a_clear_error(api, monkeypatch):
    def redirected(url, headers):
        raise HTTPError(url, 307, "Temporary Redirect", {}, BytesIO(b""))

    monkeypatch.setattr(api, "_http_json", redirected)
    assert api._fetch_api_spend("openai", SECRET) == {"ok": False, "error": "HTTP 307: redirect refused"}


def test_api_state_skips_disabled_and_unset_keys(api, fake_module, monkeypatch):
    values = {"ANTHROPIC_ADMIN_KEY": SECRET, "OPENAI_ADMIN_KEY": SECRET}
    fake_module("agent.secret_scope", get_secret=lambda name, default=None: values.get(name))
    fetched = []
    monkeypatch.setattr(api, "_fetch_api_spend",
                        lambda p, k: fetched.append((p, k)) or {"ok": True, "month_to_date_usd": 1.0})
    cfg = api.normalize_config({"disabled": ["OPENAI_ADMIN_KEY"]})
    state = api._api_state(cfg, force=True)
    assert fetched == [("anthropic", SECRET)]
    assert [k["env"] for k in state["keys"]] == ["ANTHROPIC_ADMIN_KEY"]
    assert SECRET not in json.dumps(state)
    values.clear()  # nothing set
    api._api_cache.clear()
    state = api._api_state(api.normalize_config({}), force=True)
    assert state["keys"] == [] and "Settings -> Plugins -> Plan Usage" in state["hint"]


def test_api_state_uses_cache_within_ttl(api, fake_module, monkeypatch):
    fake_module("agent.secret_scope", get_secret=lambda name, default=None: SECRET if name == "OPENAI_ADMIN_KEY" else None)
    fetched = []
    monkeypatch.setattr(api, "_fetch_api_spend",
                        lambda p, k: fetched.append(p) or {"ok": True, "month_to_date_usd": 2.0})
    cfg = api.normalize_config({})
    api._api_state(cfg, force=False)
    out = api._api_state(cfg, force=False)
    assert fetched == ["openai"]
    assert out["keys"][0]["month_to_date_usd"] == 2.0 and "fetched_at" in out["keys"][0]


def test_month_window(api):
    start, end = api._month_window(datetime(2026, 11, 30, 22, 5, tzinfo=timezone.utc))
    assert start.isoformat() == "2026-11-01T00:00:00+00:00"
    assert end.isoformat() == "2026-12-01T00:00:00+00:00"


# ── routes ───────────────────────────────────────────────────────────────────

def test_get_config_lists_accounts_and_fixed_key_slots(api, fake_module, pools):
    from conftest import FakePool

    fake_module("agent.secret_scope", get_secret=lambda name, default=None: None)
    pools["pools"]["anthropic"] = FakePool([entry("a", "Work")])
    out = api.get_config()
    assert [a["key"] for a in out["accounts"]] == ["anthropic:a"]
    assert out["accounts"][0]["section"] == "claude" and out["accounts"][0]["is_default"]
    assert [s["env"] for s in out["api_keys"]] == ["ANTHROPIC_ADMIN_KEY", "OPENAI_ADMIN_KEY"]
    assert "tok-SECRET-VALUE" not in json.dumps(out)
