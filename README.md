# Plan Usage

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) desktop plugin that puts your plan
usage in the status bar: every Claude and ChatGPT/Codex account Hermes can fall back to, Nous Portal
credits, and month-to-date API spend, in one row of chips.

| Chip | Source | Shows |
|---|---|---|
| **Nous** | your signed-in Nous Portal account, through Hermes | total usable credits (subscription + top-up), plan windows on usage-window plans, a red dot when paid access is paused |
| **ChatGPT / Codex**, one per account | every `openai-codex` account in Hermes's credential pool | plan windows % used (5-hour, weekly; a Free plan's 30-day window shows as monthly) and reset times |
| **Claude**, one per account | every `anthropic` account in Hermes's credential pool | 5-hour session and weekly % used, reset times |
| **API spend**, one per key | an optional Anthropic and/or OpenAI **Admin** key | month-to-date spend (the month in UTC; daily data, can lag a few hours) |

Hover a chip for details; click it to refresh (at most once every 30 seconds; the backend enforces
the same floor). Pool entries that are plain API keys (for example `ANTHROPIC_API_KEY` picked up
from the environment) have no plan windows, so they get no chip, but they still count when Plan
Usage works out the default and the fallback.

## Install

Once the plugin is listed in the Hermes plugin catalog:

```bash
hermes plugins install plan-usage
```

Until then, install it from this repository:

```bash
hermes plugins install ziadelboshy/hermes-plan-usage
```

Then enable it (`hermes plugins enable plan-usage`) and restart Hermes so the gateway loads the
backend. Requires Hermes 0.23.0 or newer and the Hermes desktop app.

## Settings

Everything lives under **Settings → Plugins → Plan Usage** (the command palette's
"Plan Usage: settings" opens it). The page is saved per Hermes profile:

- Switch each section (Nous, ChatGPT / Codex, Claude, API spend) on or off.
- Switch each account and each Admin key on or off, and give it a display name ("Work", "Personal").
  Without one, an account shows Hermes's pool label: the one you chose
  (`hermes auth add anthropic --label "Work"`), or the one Hermes gave it when it found the
  account itself, which is usually the account email, or the variable name (`ANTHROPIC_API_KEY`)
  for a key from the environment. Generic labels (`claude_code`, `hermes_pkce`, `device_code`, ...)
  show as "Claude 1", "ChatGPT 2", and so on.
- **☆ / ★ Default**: the starred account is the one Hermes tries first. Clicking the star moves
  that account to the top of Hermes's credential pool, the same thing
  `hermes auth priority <provider> <account> 0` does. With the default `fill_first` strategy
  Hermes moves to the next account only when the first one runs out. Hermes has its own ordering
  rules (for `anthropic` it keeps accounts added with `hermes auth add` ahead of ones it found
  itself); when a rule puts the account back, the page says so instead of reporting success.
- Poll interval: 60 to 1800 seconds (default 120).

**Settings → Plugins → Plan Usage → Agent settings** (the form Hermes builds from `plugin.yaml`)
holds the two optional secrets:

- `ANTHROPIC_ADMIN_KEY`: an Anthropic Admin key (`sk-ant-admin…`, from an Anthropic organization).
- `OPENAI_ADMIN_KEY`: an OpenAI Admin key (`sk-admin…`, organization owner).

Hermes stores them through its own credential route; the plugin reads them back through Hermes and
never shows them. A normal API key cannot read spend.

**Warnings.** When the default account reaches 90% of a limit, a notification names the account
Hermes will move to, or says there is none. When the default account is out and Hermes is on a
fallback, a second notification says so and when the default comes back. Each warning shows once per
limit window (when Hermes reports no reset time, at most once per UTC day). They only warn; Hermes
switches accounts on its own.

**Adding and removing accounts** stays with Hermes:

```bash
hermes auth add anthropic --label "Work"     # or openai-codex
hermes auth list
hermes auth remove anthropic "Work"          # index, entry id or exact label
```

## Disclosure

**Network calls made by the plugin's own code**

- `https://api.anthropic.com/v1/organizations/cost_report`, only when `ANTHROPIC_ADMIN_KEY` is set
  and both the API spend section and that key are switched on. Sends the Admin key (`x-api-key`)
  and reads this month's (UTC) daily cost buckets. At most once every 30 minutes, or on a click
  (at most once every 30 seconds).
- `https://api.openai.com/v1/organization/costs`, the same for `OPENAI_ADMIN_KEY`
  (`Authorization: Bearer`).
- Redirects are refused, so a key is never re-sent to another URL.

**Requests the plugin asks Hermes to make** (Hermes holds the credential and makes the request)

- Nous Portal credits: `agent.account_usage.build_credits_view`, at most once every 5 minutes.
  Hermes calls your Nous Portal (`portal.nousresearch.com` by default) with its own sign-in.
- Usage windows: the desktop half calls the gateway's `model.options` RPC once per poll, the same
  call the model picker makes. It returns Hermes's cached per-account usage. Hermes may refresh that
  cache in the background from the Anthropic and ChatGPT usage endpoints, as it does when you open
  the model picker. The plugin never receives a subscription token.

**What it reads**

- Hermes's credential-pool metadata through the public `agent.credential_pool` API: entry ids,
  sources, labels, auth type, status and order. The plugin's code never touches a token field (a
  test enforces this). Reading the pool is `load_pool()`, which is Hermes's own code: it can seed
  or refresh rows and write Hermes's auth store, as it does for any caller.
- Its two declared secrets, through `agent.secret_scope.get_secret`. It never opens `.env`,
  `auth.json` or any other credential file, and never reads `os.environ` for secrets.
- Its own settings file.

**What it writes**

- `<HERMES_HOME>/plugin-data/plan-usage/settings.json`: section and account switches, display
  names, poll interval. No secrets.
- Desktop plugin storage (`hermes.plugin.plan-usage.*`): the ids of warnings already shown.
- Only when you click the star: the credential-pool order, through Hermes's
  `CredentialPool.move_entry` (Hermes writes the order to the auth store that owns the accounts).
  In a named profile that borrows the root profile's accounts (it has none of its own), the star
  reorders those shared accounts, so the new order also applies to the root profile and every
  other profile that borrows them.

**What it does not do**: no telemetry or usage reporting, no subprocesses or shell commands, no
tools, hooks or per-turn cost, no self-updating. Fetches run in short-lived background threads that
start only when the status bar polls; nothing runs between polls. Key values and tokens are never
sent to the desktop, logged or stored by the plugin.

## How it differs from other usage plugins in the catalog

- **provider-usage**: per-provider quota and balance chips with a popover, for many providers.
  Per its catalog entry it resolves provider credentials itself (Hermes helpers and profile
  secrets, else the profile's `.env` and vendor CLI login files) and calls each provider's usage
  endpoint. Plan Usage's code opens no credential file and calls no usage endpoint: windows
  come from Hermes's own cache. It adds a named chip per pooled account with a default star and
  fallback warnings, and Admin-key organization spend.
- **billing**: a Nous Portal widget (credits and plan-usage percentage). Plan Usage shows the same
  Nous figures next to the Claude, ChatGPT and API-spend chips; if Nous is all you need, billing is
  the smaller plugin.
- **model-usage**: ring gauges for Claude and Codex (chat context, 5-hour, weekly). Plan Usage adds
  Nous Portal credits, Admin-key API spend, and a named chip per pooled account.
- **model-usage-status**: Claude and Codex windows from the vendor CLIs, macOS only. Plan Usage
  spawns nothing, reads no CLI login, and is not tied to one OS.
- **ai-usage-tracker** and **limits-monitor**: quota windows for many providers in a page or pane.
  Plan Usage stays in the status bar and adds what they do not show: month-to-date API spend from
  the Anthropic and OpenAI organization cost reports, Nous Portal credits next to your
  subscriptions, and per-account names, a default star and fallback warnings for Hermes's
  credential pool.

## Known issues

- **Claude at 1% can show as 100%.** Hermes core reads Anthropic's `utilization` as a 0–1 fraction,
  so exactly 1% used shows as 100% and can trigger a warning. Plan Usage shows what Hermes reports.
  Upstream issue
  [NousResearch/hermes-agent#145660](https://github.com/NousResearch/hermes-agent/issues/145660)
  (open; fix PRs #139916 and #145663 are open).
- Hermes reports usage per account, not per credential, and labels each account with its pool
  label. Plan Usage matches the two by label in pool order, so several credentials of one ChatGPT
  account show as one chip, and two different accounts with the same label show in pool order.
  Give each account its own label (`--label`) to keep them apart.
- An account whose limits Hermes has not fetched yet shows as `Name …` until the next poll.
- Names and switches for accounts Hermes found itself are keyed by where Hermes found them
  (`claude_code`, `hermes_pkce`, `env:NAME`), because Hermes gives those rows a new id on each load
  in a named profile. Logging into a different account through the same route keeps the old name.
- The backend reloads only when the Hermes gateway restarts; the desktop half reloads with a
  desktop plugin rescan or an app restart.

## Development

```text
plugin.yaml               manifest; config_schema declares the two optional secrets
__init__.py               register(): a no-op, the plugin has no tools or hooks
dashboard/plugin_api.py   backend routes under /api/plugins/plan-usage/*
dashboard/manifest.json   backend manifest
desktop/plugin.js         desktop half: status-bar chips, Settings → Plugins page, palette commands
tests/                    offline pytest suite
```

The tests are offline (no network, no real credentials, no real Hermes home; Hermes core modules
are faked) and need only `pytest`:

```bash
python -m pytest -q
```

They also pass with a Hermes checkout on `PYTHONPATH`. Validate the package the way the catalog
does:

```bash
hermes plugins validate .
```

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Ziad Osama El-Boshy.
