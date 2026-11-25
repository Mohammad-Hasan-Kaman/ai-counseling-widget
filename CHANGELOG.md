# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0]

### Changed

- Plan tiers restructured from four to three (migration 16): the old
  Standard tier (operator's generic model) was removed and its features
  merged into the tier above it; tier names shifted one step down —
  Base is unchanged, the old Professional (DeepSeek) is now **Standard**,
  and the old Complete tier is now **Professional**. Feature sets and
  limits travel with each plan's content; the periodic-report gate now
  reads the stored tier (`pro`), and Standard's old limits (500 smart
  messages / 50 introductions) were *not* carried over — the merged
  Standard keeps 1,500 / 80.
- Landing page, pricing page and the admin-panel shell were redesigned
  (warm paper neutrals, the brand green as the single accent, hairline
  separators instead of shadows, no decorative gradients or emoji).

### Added

- Hardened super-admin editing of per-plan monthly limits (smart
  messages, counselor introductions): values are range-validated
  (`-1` = unlimited, oversized values rejected with a form error
  instead of a database overflow), and `plan.create` / `plan.update`
  audit entries now record the limit values — updates include the
  previous values. Panel end-to-end tests cover edit → store →
  enforcement → restore.

## [2.0.0]

### Added

- Multi-tenant SaaS edition: one deployment serves many counseling centers
  ("tenants"), each with its own API key, embed domain allow-list, intake flow
  and panel logins.
- Subscription plans (base / standard / pro / full) with monthly request
  quotas, top-up packages, and a super-admin console for plans, tenants,
  quotas, top-up approvals and few-shot examples.
- Optional per-plan LLM mode with three plan modes — rule engine only, LLM
  only, and LLM with rule-engine fallback. The LLM interprets free-text
  answers and maps them onto flow choices; provider API keys are stored
  Fernet-encrypted.
- Audit log, plus a reporting page with CSV export for tenant admins.
- Versioned database migrations (`app/migrations.py`).

### Changed

- Chat rate limiting is now database-backed (shared correctly across
  workers): 60 requests per 60 seconds per IP/session. The panel login limit
  uses the same limiter (10 attempts per 5 minutes).
- The GHQ-28 "elevated" band now starts at a total score of 24, aligned with
  the admin banding and with the companion Bale bot.

## [1.0.0]

### Added

- The original open-core widget: an embeddable intake chat (a single script
  tag) that walks a visitor through a configurable intake flow.
- GHQ-28 wellbeing screening — 28 questions, four subscales — scored locally.
- A deterministic local matching engine (concept extraction from free text,
  weighted scoring, feedback learning) with no third-party AI service.
- A scheduled crawler that refreshes counselor availability from the center's
  public team page.
- A single-center admin panel with inbox, leads, counselor spreadsheet
  uploads and statistics.
