"""Plan Usage backend: plan/credit/spend readouts for the desktop status bar.

Sections (each can be switched off on the plugin's Settings -> Plugins -> Plan Usage page):
  claude  - the Anthropic accounts in Hermes's credential pool (names, order, default)
  codex   - the openai-codex accounts in Hermes's credential pool (names, order, default)
  nous    - Nous Portal subscription + top-up credits, through Hermes's own credits view
  api     - month-to-date API spend from an Anthropic and/or OpenAI *Admin* key

Usage windows (5-hour / weekly percentages) are NOT fetched here: the desktop half reads them from
the gateway's ``model.options`` RPC, which serves Hermes core's own per-account usage cache. This
backend never reads, holds or sends a subscription token.

Credentials: the two Admin keys are declared ``config_schema`` secrets (``ANTHROPIC_ADMIN_KEY`` /
``OPENAI_ADMIN_KEY``, set in Settings -> Plugins -> Plan Usage) and are read only with
``agent.secret_scope.get_secret``. The plugin never opens ``.env`` or ``auth.json``. Key values are
never returned, logged or stored by this plugin.

Writes: the plugin's own settings file under ``<HERMES_HOME>/plugin-data/plan-usage/``, and, only
when the user clicks the star, the credential-pool order through Hermes's public
``CredentialPool.move_entry`` (same effect as ``hermes auth priority``).

Account keys: settings (names, switches) are keyed ``<provider>:<entry id>`` for accounts added with
``hermes auth add`` and ``<provider>:<source>`` (``claude_code``, ``hermes_pkce``, ``env:NAME``...) for
rows Hermes seeds itself, because in a named profile Hermes gives seeded rows a new id on each load.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from fastapi import APIRouter
except Exception:  # pragma: no cover - lets the pure helpers import without fastapi
    class APIRouter:  # type: ignore[no-redef]
        def get(self, *_a, **_k):
            return lambda fn: fn

        post = get

logger = logging.getLogger("plan_usage")
router = APIRouter()

PLUGIN_ID = "plan-usage"
SETTINGS_FILE = "settings.json"

SECTIONS = ("nous", "codex", "claude", "api")  # display order
POOL_PROVIDERS = {"claude": "anthropic", "codex": "openai-codex"}
# Fixed Admin-key slots. Each is a declared config_schema secret (plugin.yaml), read via get_secret.
ADMIN_KEYS = (
    {"env": "ANTHROPIC_ADMIN_KEY", "provider": "anthropic"},
    {"env": "OPENAI_ADMIN_KEY", "provider": "openai"},
)
ADMIN_ENVS = tuple(k["env"] for k in ADMIN_KEYS)
NAME_MAX = 40
KEY_MAX = 64      # longest account key / switch id kept in settings
MAX_ITEMS = 200   # most names / switched-off ids kept in settings

DEFAULT_CONFIG: dict[str, Any] = {
    "sections": {s: True for s in SECTIONS},
    "names": {},            # account key (see account_key) -> display name
    "key_names": {},        # ANTHROPIC_ADMIN_KEY / OPENAI_ADMIN_KEY -> display name
    "disabled": [],         # account keys and Admin-key names switched off
    "poll_seconds": 120,
}

NOUS_TTL_S = 300
API_TTL_S = 1800
HTTP_TIMEOUT_S = 15
FORCE_FLOOR_S = 30  # a forced refresh (click / palette) bypasses the caches at most this often

_lock = threading.RLock()
_config_lock = threading.RLock()
_nous_cache: dict[str, Any] = {}
_api_cache: dict[str, dict] = {}
_inflight: set[str] = set()
_last_forced = 0.0


# ── config (plugin-owned, in Hermes plugin storage) ──────────────────────────

def _config_path() -> Path:
    """``<HERMES_HOME>/plugin-data/plan-usage/settings.json`` (profile-scoped, outside the repo)."""
    from plugins.plugin_storage import plugin_data_dir

    return Path(plugin_data_dir(PLUGIN_ID)) / SETTINGS_FILE


def _clean_name(value: Any) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
    return text[:NAME_MAX]


_ACCOUNT_KEY_RE = re.compile(r"^[a-z0-9-]+:[A-Za-z0-9_.:@+/-]+$")


def normalize_config(raw: Any) -> dict:
    """Coerce any stored/posted config into the known shape; unknown keys are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    sections = raw.get("sections") if isinstance(raw.get("sections"), dict) else {}
    for s in SECTIONS:
        if s in sections:
            cfg["sections"][s] = bool(sections[s])
    names = raw.get("names") if isinstance(raw.get("names"), dict) else {}
    for k, v in names.items():
        if len(cfg["names"]) >= MAX_ITEMS:
            break
        key, name = str(k), _clean_name(v)
        if name and len(key) <= KEY_MAX and _ACCOUNT_KEY_RE.match(key):
            cfg["names"][key] = name
    key_names = raw.get("key_names") if isinstance(raw.get("key_names"), dict) else {}
    cfg["key_names"] = {str(k).upper(): _clean_name(v) for k, v in key_names.items()
                        if str(k).upper() in ADMIN_ENVS and _clean_name(v)}
    disabled = []
    for item in raw.get("disabled") or []:
        if len(disabled) >= MAX_ITEMS:
            break
        text = str(item or "").strip()
        if text and len(text) <= KEY_MAX and re.match(r"^[A-Za-z0-9_.:@+/-]+$", text) and text not in disabled:
            disabled.append(text)
    cfg["disabled"] = disabled
    try:
        cfg["poll_seconds"] = max(60, min(1800, int(raw.get("poll_seconds", 120))))
    except (TypeError, ValueError):
        cfg["poll_seconds"] = 120
    return cfg


def load_config() -> dict:
    with _config_lock:
        try:
            path = _config_path()
        except Exception:
            logger.debug("plan-usage: plugin storage unavailable, using defaults", exc_info=True)
            return normalize_config({})
        try:
            return normalize_config(json.loads(path.read_text("utf-8")))
        except FileNotFoundError:
            return normalize_config({})
        except Exception:
            logger.warning("plan-usage: unreadable settings file, using defaults")
            return normalize_config({})


def save_config(cfg: dict) -> dict:
    cfg = normalize_config(cfg)
    with _config_lock:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2), "utf-8")
        os.replace(tmp, path)
    return cfg


# ── Claude / Codex accounts: metadata from Hermes's credential pool ─────────

_AUTO_LABEL_RE = re.compile(
    r"^(device_code|hermes_pkce|claude_code|codex_cli|gh_cli|manual(:.*)?|env:.*|[a-z0-9-]+-(oauth|api-key|key)-\d+)$",
    re.I)


def default_account_name(provider: str, label: str, index: int) -> str:
    """Pool label when the user chose one (hermes auth add --label), else 'Claude 1' / 'ChatGPT 1'."""
    if label and not _AUTO_LABEL_RE.match(label):
        return label[:NAME_MAX]
    return f"{'Claude' if provider == 'anthropic' else 'ChatGPT'} {index}"


def _is_manual(source: str) -> bool:
    src = (source or "").strip().lower()
    return src == "manual" or src.startswith("manual:")


def account_keys(provider: str, entries: list) -> list[str]:
    """Stable settings key per pool entry, in pool order.

    Accounts added with ``hermes auth add`` (source ``manual``/``manual:*``) keep their entry id
    across loads, so they are keyed by id. Rows Hermes seeds itself (the Claude CLI login
    ``claude_code``, ``hermes_pkce``, ``env:NAME``...) are one row per source, and in a named profile
    they get a new random id on every ``load_pool()``, so they are keyed by source. A repeated
    source falls back to the entry id."""
    keys, seen = [], set()
    for entry in entries:
        source = str(getattr(entry, "source", "") or "")
        key = f"{provider}:{source}" if source and not _is_manual(source) else f"{provider}:{entry.id}"
        if key in seen:
            key = f"{provider}:{entry.id}"
        seen.add(key)
        keys.append(key)
    return keys


def pool_accounts(provider: str, names: dict, disabled: Optional[set] = None) -> dict:
    """One row per pool entry, in Hermes's priority order. Metadata only: never a token.

    Every entry is returned (``enabled`` says whether it is shown) because the desktop half joins
    these rows with the gateway's per-account usage by order and label, and because the
    default/fallback order is computed over all accounts (hiding one here does not stop Hermes
    from falling back to it)."""
    disabled = disabled or set()
    try:
        from agent.credential_pool import STATUS_DEAD, get_pool_strategy, load_pool
    except Exception as exc:  # Hermes moved its public API: degrade, don't crash the bar
        return {"ok": False, "error": f"Hermes credential pool API unavailable ({type(exc).__name__})",
                "accounts": []}
    try:
        entries = list(load_pool(provider).entries())
    except Exception:
        logger.debug("plan-usage: pool read failed for %s", provider, exc_info=True)
        return {"ok": False, "error": "could not read Hermes credential pool", "accounts": []}
    try:
        strategy = str(get_pool_strategy(provider))
    except Exception:
        strategy = "fill_first"
    rows = []
    for index, (entry, key) in enumerate(zip(entries, account_keys(provider, entries)), start=1):
        label = str(getattr(entry, "label", "") or "")
        rows.append({
            "key": key,
            "provider": provider,
            "pool_label": label,
            "name": names.get(key) or default_account_name(provider, label, index),
            "custom_name": bool(names.get(key)),
            "enabled": key not in disabled,
            "dead": getattr(entry, "last_status", None) == STATUS_DEAD,
            # An API-key row (e.g. env:ANTHROPIC_API_KEY) has no plan windows: not shown as a chip,
            # but it still counts for the default / fallback order.
            "api_key": str(getattr(entry, "auth_type", "") or "") == "api_key",
            "is_default": False,
        })
    first_live = next((r for r in rows if not r["dead"]), None)
    if first_live is not None:
        first_live["is_default"] = True
    hint = None if rows else f"No {provider} account in Hermes. Add one with: hermes auth add {provider} --label \"Name\""
    return {"ok": True, "accounts": rows, "strategy": strategy, "hint": hint}


def set_default_account(key: str) -> dict:
    """Move an account to the top of Hermes's pool order (same as `hermes auth priority <p> <id> 0`).

    The settings key is resolved to the entry's *current* id at click time (seeded rows can change id
    between loads). Hermes may re-sort after the move (anthropic keeps manually added credentials
    ahead of seeded ones), so success is reported only when the entry really ends up first."""
    key = str(key or "")
    provider, _, rest = key.partition(":")
    if provider not in POOL_PROVIDERS.values() or not rest:
        raise ValueError("unknown account")
    from agent.credential_pool import get_pool_strategy, load_pool

    pool = load_pool(provider)
    entries = list(pool.entries())
    target = next((e for e, k in zip(entries, account_keys(provider, entries)) if k == key), None)
    if target is None:
        raise ValueError("that account is no longer in Hermes")
    moved = pool.move_entry(target.id, 0)
    if moved is None:
        raise ValueError("Hermes could not reorder that account")
    priority = getattr(moved, "priority", 0)
    if priority != 0:
        why = ("Hermes keeps manually added Anthropic accounts (hermes auth add) ahead of ones it "
               "detects itself" if provider == "anthropic" else "Hermes re-sorted the pool")
        return {"ok": False, "note": f"Not made the default: {why}, so this account stays at #{priority + 1}."}
    out: dict[str, Any] = {"ok": True}
    strategy = get_pool_strategy(provider)
    if strategy != "fill_first":
        out["note"] = (f"{provider} uses the {strategy} strategy, so Hermes spreads work across accounts and "
                       "does not really have a default. Set credential_pool_strategies."
                       f"{provider}: fill_first in config.yaml for default-then-fallback behaviour.")
    return out


# ── Nous Portal (Hermes's own credits view; the plugin makes no request) ─────

_MONEY_RE = re.compile(
    r"^\s*(Subscription credits|Top-up credits|Total usable|Balance|Credits)\s*:\s*\$?\s*(-?[0-9][0-9,]*\.?[0-9]*)",
    re.I)
_NOUS_SKIP = ("Top up", "(or run", "Provider:", "\U0001f4c8")


def parse_nous_lines(lines: list[str]) -> dict:
    """Pull the dollar figures out of Hermes's rendered Nous credit lines."""
    money: dict[str, float] = {}
    for line in lines:
        m = _MONEY_RE.match(line)
        if m:
            money[m.group(1).lower()] = float(m.group(2).replace(",", ""))
    total = money.get("total usable")
    if total is None:
        parts = [money[k] for k in ("subscription credits", "top-up credits") if k in money]
        total = sum(parts) if parts else money.get("balance", money.get("credits"))
    return {
        "subscription_usd": money.get("subscription credits"),
        "topup_usd": money.get("top-up credits"),
        "total_usd": total,
    }


def _fetch_nous() -> dict:
    from agent.account_usage import build_credits_view

    view = build_credits_view(markdown=False, timeout=HTTP_TIMEOUT_S)
    if not getattr(view, "logged_in", False):
        return {"ok": False,
                "error": "not signed in to Nous Portal (hermes auth add nous), or the Portal is unreachable"}
    lines = [str(ln) for ln in (getattr(view, "balance_lines", ()) or ())
             if str(ln).strip() and not str(ln).lstrip().startswith(_NOUS_SKIP)]
    return {"ok": True, "lines": lines[:8], **parse_nous_lines(lines),
            "depleted": bool(getattr(view, "depleted", False))}


# ── API spend (Admin keys, declared config_schema secrets) ──────────────────

def _secret(name: str) -> str:
    """Read one declared secret through Hermes's profile-aware route. Never .env, never os.environ."""
    if name not in ADMIN_ENVS:
        raise ValueError("not a declared Plan Usage secret")
    try:
        from agent.secret_scope import get_secret

        return str(get_secret(name) or "").strip()
    except Exception:  # no scope (multiplexed gateway) or Hermes too old: treat as not set
        logger.debug("plan-usage: get_secret(%s) unavailable", name, exc_info=True)
        return ""


def admin_key_slots(cfg: dict) -> list[dict]:
    """The two fixed Admin-key slots with presence, enabled state and display name. Never values."""
    disabled = set(cfg.get("disabled", []))
    out = []
    for slot in ADMIN_KEYS:
        env = slot["env"]
        out.append({**slot, "present": bool(_secret(env)), "enabled": env not in disabled,
                    "name": cfg.get("key_names", {}).get(env) or _default_key_name(slot["provider"])})
    return out


def _default_key_name(provider: str) -> str:
    return "Anthropic API" if provider == "anthropic" else "OpenAI API"


def _month_window(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


class _NoRedirect(HTTPRedirectHandler):
    """Refuse every redirect: urllib would otherwise re-send x-api-key / Authorization to the new URL.
    The 3xx then surfaces as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def _http_json(url: str, headers: dict) -> dict:
    req = Request(url, headers={**headers, "Accept": "application/json", "User-Agent": "hermes-plan-usage/0.2"})
    with _OPENER.open(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sum_anthropic_cost(pages: list[dict]) -> float:
    """Anthropic cost_report amounts are decimal strings in cents."""
    cents = 0.0
    for page in pages:
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                try:
                    cents += float(result.get("amount") or 0)
                except (TypeError, ValueError):
                    continue
    return round(cents / 100.0, 2)


def sum_openai_cost(pages: list[dict]) -> float:
    """OpenAI /organization/costs amounts are {value, currency} in dollars."""
    dollars = 0.0
    for page in pages:
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                amount = result.get("amount") or {}
                try:
                    dollars += float(amount.get("value") or 0)
                except (TypeError, ValueError, AttributeError):
                    continue
    return round(dollars, 2)


def _paged(first_url: str, headers: dict, page_param: str, max_pages: int = 6) -> list[dict]:
    pages, url = [], first_url
    for _ in range(max_pages):
        page = _http_json(url, headers)
        pages.append(page)
        nxt = page.get("next_page")
        if not page.get("has_more") or not nxt:
            break
        sep = "&" if "?" in first_url else "?"
        url = f"{first_url}{sep}{urlencode({page_param: nxt})}"
    return pages


def _fetch_api_spend(provider: str, key: str) -> dict:
    if not key:
        return {"ok": False, "error": "key not set"}
    start, end = _month_window()
    try:
        if provider == "anthropic":
            qs = urlencode({"starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "ending_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "bucket_width": "1d", "limit": 31})
            pages = _paged(f"https://api.anthropic.com/v1/organizations/cost_report?{qs}",
                           {"x-api-key": key, "anthropic-version": "2023-06-01"}, "page")
            spend = sum_anthropic_cost(pages)
        else:
            qs = urlencode({"start_time": int(start.timestamp()), "bucket_width": "1d", "limit": 31})
            pages = _paged(f"https://api.openai.com/v1/organization/costs?{qs}",
                           {"Authorization": f"Bearer {key}"}, "page")
            spend = sum_openai_cost(pages)
    except HTTPError as exc:
        hint = {401: "key rejected", 403: "not an Admin key / no org access", 404: "endpoint not found",
                429: "rate limited"}.get(exc.code, "redirect refused" if 300 <= exc.code < 400 else "request failed")
        return {"ok": False, "error": f"HTTP {exc.code}: {hint}"}
    except (URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"network error ({type(exc).__name__})"}
    except Exception as exc:  # never echo payloads that could contain secrets
        return {"ok": False, "error": f"unexpected response ({type(exc).__name__})"}
    return {"ok": True, "month_to_date_usd": spend, "period_start": start.date().isoformat(),
            "note": "Month to date in UTC; daily data, can lag a few hours."}


# ── background refresh helpers ───────────────────────────────────────────────

def _spawn(tag: str, fn: Callable[[], None]) -> None:
    """One short-lived daemon thread per fetch, deduplicated by tag; started only by a poll."""
    with _lock:
        if tag in _inflight:
            return
        _inflight.add(tag)

    def run():
        try:
            fn()
        except Exception:
            logger.debug("plan-usage: %s refresh failed", tag, exc_info=True)
        finally:
            with _lock:
                _inflight.discard(tag)

    # Carry the caller's contextvars (Hermes's secret scope / profile home) into the worker, so
    # get_secret and plugin storage resolve for the same profile under multi-profile hosting.
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(run,), name=f"plan-usage-{tag}", daemon=True).start()


def _nous_state(force: bool) -> dict:
    now = time.time()
    with _lock:
        cached = dict(_nous_cache)
    if force or not cached or now - cached.get("_at", 0) > NOUS_TTL_S:
        def job():
            try:
                result = _fetch_nous()
            except Exception as exc:
                result = {"ok": False, "error": f"Nous credits unavailable ({type(exc).__name__})"}
            with _lock:
                _nous_cache.clear()
                _nous_cache.update(result, _at=time.time())
        _spawn("nous", job)
    if not cached:
        return {"ok": None, "loading": True}
    cached["fetched_at"] = datetime.fromtimestamp(cached.pop("_at"), timezone.utc).isoformat()
    return cached


def _api_state(cfg: dict, force: bool) -> dict:
    now = time.time()
    rows = []
    for slot in admin_key_slots(cfg):
        if not slot["enabled"] or not slot["present"]:
            continue  # an unset slot is not shown in the bar (the settings page lists it)
        env, provider = slot["env"], slot["provider"]
        row = {k: slot[k] for k in ("env", "provider", "name", "present")}
        with _lock:
            cached = dict(_api_cache.get(env, {}))
        if force or not cached or now - cached.get("_at", 0) > API_TTL_S:
            def job(env=env, provider=provider):
                result = _fetch_api_spend(provider, _secret(env))
                with _lock:
                    _api_cache[env] = {**result, "_at": time.time()}
            _spawn(f"api:{env}", job)
        if cached:
            cached["fetched_at"] = datetime.fromtimestamp(cached.pop("_at"), timezone.utc).isoformat()
            rows.append({**row, **cached})
        else:
            rows.append({**row, "ok": None, "loading": True})
    hint = None if rows else "Set ANTHROPIC_ADMIN_KEY or OPENAI_ADMIN_KEY in Settings -> Plugins -> Plan Usage."
    return {"ok": True, "keys": rows, "hint": hint}


def _allow_force(force: bool) -> bool:
    """At most one cache-bypassing refresh per FORCE_FLOOR_S, however often the UI asks."""
    global _last_forced
    if not force:
        return False
    now = time.time()
    with _lock:
        if now - _last_forced < FORCE_FLOOR_S:
            return False
        _last_forced = now
        return True


def build_status(force: bool = False) -> dict:
    force = _allow_force(force)
    cfg = load_config()
    disabled = set(cfg["disabled"])
    out: dict[str, Any] = {"sections": cfg["sections"], "poll_seconds": cfg["poll_seconds"],
                           "generated_at": datetime.now(timezone.utc).isoformat()}
    for section, provider in POOL_PROVIDERS.items():
        if cfg["sections"][section]:
            out[section] = pool_accounts(provider, cfg["names"], disabled)
    if cfg["sections"]["nous"]:
        out["nous"] = _nous_state(force)
    if cfg["sections"]["api"]:
        out["api"] = _api_state(cfg, force)
    return out


# ── routes ───────────────────────────────────────────────────────────────────

@router.get("/status")
def get_status():
    return build_status(force=False)


@router.post("/refresh")
def post_refresh():
    return build_status(force=True)


@router.get("/config")
def get_config():
    cfg = load_config()
    accounts = []
    for section, provider in POOL_PROVIDERS.items():
        data = pool_accounts(provider, cfg["names"], set(cfg["disabled"]))
        for row in data.get("accounts", []):
            accounts.append({k: row[k] for k in ("key", "provider", "pool_label", "name", "custom_name",
                                                  "enabled", "is_default", "dead", "api_key")} | {"section": section})
    return {"config": cfg, "accounts": accounts, "api_keys": admin_key_slots(cfg)}


@router.post("/config")
def post_config(body: dict):
    raw = body.get("config") if isinstance(body, dict) and "config" in body else body
    saved = save_config(raw if isinstance(raw, dict) else {})
    with _lock:
        _api_cache.clear()
    return {"ok": True, "config": saved}


@router.post("/default")
def post_default(body: dict):
    body = body if isinstance(body, dict) else {}
    try:
        return set_default_account(str(body.get("key") or ""))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("plan-usage: set default failed (%s)", type(exc).__name__)
        return {"ok": False, "error": f"could not set default ({type(exc).__name__})"}
