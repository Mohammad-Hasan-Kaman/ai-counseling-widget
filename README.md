# AI Counseling Widget v2 — Multi-Tenant SaaS Edition

A multi-tenant, Persian (RTL) counseling-triage platform: an embeddable chat
widget that walks a visitor through a guided intake conversation, administers
the GHQ-28 questionnaire, and matches them with the most suitable counselor —
with subscription plans, usage quotas, and optional per-plan LLM assistance
built in.

This is v2 of the ai-counseling-widget. The intake flow, GHQ-28 scoring,
and the matching engine are the same core as v1 (still available under the
`v1.0.0` tag and release); v2 adds the business layer that turns it into a
hosted product: subscription plans, usage quotas, and per-plan LLM
assistance.

## What it does

A counseling center (a *tenant*) embeds one line of JavaScript on its site.
Visitors chat with the widget instead of filling a form:

1. A per-tenant conversation flow collects who the intake is for, contact
   details, topic, age, preferences, and branch.
2. The GHQ-28 questionnaire (28 items, four subscales) is administered and
   scored server-side.
3. The matching engine ranks the center's counselors by specialty fit,
   gender preference, age range, branch, GHQ signals, and **live free
   slots**, and introduces the top matches with reasons.
4. Every request is stored as a lead in the tenant's admin panel — even when
   no counselor matches.

## Features

**Platform / multi-tenancy**

- Unlimited tenants, each with its own API key, allowed domains, flow
  configuration, admin users, and data isolation enforced by `tenant_id`
  on every read path.
- Super-admin panel: tenant management, plan management, top-up approvals,
  audit log.
- Tenant admin panel: dashboard with live crawl health, leads inbox,
  consultant management (Excel upload), branch management, flow editor,
  announcements, periodic reports.
- Versioned database migrations; DB-backed rate limiting that survives
  restarts and multiple workers.

**Matching and intake**

- Rule-based matching engine (`SpiralMatchEngine`): clinical-concept
  lexicon, GHQ-28 subscale signals, per-tenant self-learning weights from
  past outcomes, branch alias handling, and a relaxation cascade so a
  visitor is never left with an empty result.
- Availability crawler: periodically reads the primary tenant's public
  team page and per-counselor schedule pages into per-tenant free-slot
  data, with a DB lease so multiple workers never crawl at once.

**Plans, quotas, top-ups (new in v2)**

- Subscription plans with three AI modes:
  - **Weak** — rule-based engine only, no external AI.
  - **Medium** — LLM-driven conversation and matching.
  - **Advanced** — LLM first, with automatic fallback to the rule engine
    when the LLM is unreachable.
- Per-plan quotas: monthly message quota and counselor-introduction
  quota, metered per tenant, with monthly reset, plan upgrades, and a
  full plan-change history.
- Top-ups: the operator defines top-up packages; tenants request them
  from their panel and the super admin approves them — no payment-gateway
  dependency in code.
- Per-plan LLM configuration: any OpenAI-compatible endpoint
  (`base_url` + model), with the API key stored **Fernet-encrypted**
  (key derived from `SECRET_KEY`) and a built-in connection test and
  model listing from the plan form.

**Widget**

- One-line embed, fully RTL Persian UI, per-tenant branding through the
  flow's welcome text, session restore, announcements, and a
  domain/referrer check so a tenant's key only works on its own sites.

## How v2 differs from v1

| Area | v1 | v2 (current) |
| --- | --- | --- |
| AI | Local rule engine only | Rule engine + optional per-plan LLM (3 modes, fallback) |
| Business model | None | Plans, monthly quotas, metering, top-ups, upgrades |
| LLM keys | — | Per plan, encrypted at rest |
| Operator tooling | Basic panels | Super-admin plans/top-ups, audit log, periodic reports |
| Crawling | Basic | Lease-based crawl state shared across workers |
| Schema changes | Ad-hoc | Versioned migrations |

## Quick start

```bash
git clone <this repo> && cd ai-counseling-widget
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set SECRET_KEY, ADMIN_PANEL_PASSWORD, SUPER_ADMIN_PASSWORD

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On first boot the app initializes its databases and seeds:

- a **demo tenant** — «مرکز مشاوره نمونه», slug `demo` — with a complete
  intake flow (the seeded tenant is also the crawler's primary tenant);
- a tenant admin user `demo` (password = `ADMIN_PANEL_PASSWORD`);
- a super admin (username/password from `.env`).

Then:

- Widget demo: http://127.0.0.1:8000/widget?key=<demo tenant API key>
  (the key is printed in the tenant row — fetch it from the super-admin
  panel or the `tenants` table)
- Tenant panel: http://127.0.0.1:8000/admin/login
- Health check: http://127.0.0.1:8000/api/chat/health

Example consultant data lives in `data/*.example.json`; copy
`data/mapping.example.json` to `data/mapping.json` (or set `MAPPING_JSON`
/ `PROFILES_JSON`) to try matching with fictional counselors. Real data
files are git-ignored by design.

## Configuration

Everything is environment-driven (`.env`); see `.env.example` for the
full list. The important ones:

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Session signing + derives the encryption key for stored LLM API keys |
| `ADMIN_PANEL_PASSWORD` | Password of the seeded demo tenant's admin |
| `SUPER_ADMIN_USERNAME` / `SUPER_ADMIN_PASSWORD` | Platform operator login |
| `CORS_ORIGINS` | Origins allowed to call the chat API |
| `WIDGET_ORIGIN` | The site the widget is embedded on |
| `TEAM_PAGE_URL` | Primary tenant's public team page (crawler source + profile links) |
| `CRAWLER_ENABLED` | `1`/`0` — scheduled availability crawl |
| `COOKIE_SECURE` | `1` behind HTTPS so panel cookies are HTTPS-only |

## Embed

```html
<script src="https://your-host/static/widget.js"
        data-origin="https://your-host"
        data-key="TENANT_API_KEY" async></script>
```

## Tests

264 tests cover the matching engine, flow state machine, GHQ scoring,
plans/tiers/upgrades, metering, monthly quotas, top-ups, per-tenant LLM
configuration, crawl leasing, rate limiting, and tenant isolation:

```bash
pip install -r requirements-dev.txt
pytest
```

Tests run fully offline against throwaway databases (see
`tests/conftest.py`); LLM calls are stubbed.

## Deployment

A `Dockerfile` and `docker-compose.yml` are included (the app listens on
port 8000; put it behind an HTTPS reverse proxy and set `COOKIE_SECURE=1`).
`DEPLOY.md` documents the production checklist. The `tools/` directory
holds operator utilities (boot probe, HTTP checks, DB inspection,
orphan pruning, appointment backfill).

## Notes and limitations

- The intake is a **screening/triage aid, not a diagnosis**; the flow
  tells the visitor this up front.
- GHQ-28 is a standardized instrument (Goldberg); review its licensing
  for your jurisdiction before commercial use.
- Prices are operator-defined data (plans carry a `price_rials` field);
  no prices, payment gateway, or real tenant data ship in this repo.

## License

MIT — see [LICENSE](LICENSE).
