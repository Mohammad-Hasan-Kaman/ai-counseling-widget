# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Repository renamed to **`ai-counseling-widget`** — an AI-first, brand-neutral public name (old URLs redirect).
- README rewritten with an "AI at the core" section describing the triage assistant,
  GHQ-28 screening, clinical concept extraction, consultant matching and self-learning weights.
- Repository documentation rewritten in English (README, DEPLOY, `.env.example` comments).
- All developer comments and docstrings translated to English; end-user UI text stays Persian.
- Added `LICENSE` (MIT), `CONTRIBUTING.md`, `CHANGELOG.md` and `docs/` screenshots.
- Untracked local tooling directory (`.claude/`) from version control.

### Fixed
- Lead registration now happens *before* the matching step, so a request is stored even
  when no consultant matches (previously the lead was silently dropped).
- Fresh-database boot order: widget session tables are created before the tenant
  column migration, so a brand-new install starts without errors.

## [1.0.0] — initial public release

### Added
- Multi-tenant chat widget with per-tenant API keys, admin panel and domain allow-list.
- Configurable admission flow (welcome message, numbered steps, phone/number/choice inputs).
- GHQ-28 screening: 28 questions across four subscales, scored server-side.
- Internal matching engine with per-tenant profile cache, concept extraction and
  learning weights updated from panel feedback.
- Availability crawler (default every 2 hours) with manual trigger from the dashboard.
- Persian Excel export for leads and consultant uploads with data-validity report.
- Health endpoint, rate limiting, signed session cookies and security headers.
- Docker / docker-compose deployment and landing page.
