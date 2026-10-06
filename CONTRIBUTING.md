# Contributing to AI Counseling Widget

Thanks for your interest in improving this project. This repository is a snapshot of a
production Persian-language chat widget for counseling centers, so please keep changes
focused and backward compatible.

## Development setup

```bash
git clone https://github.com/Mohammad-Hasan-Kaman/ai-counseling-widget.git
cd ai-counseling-widget

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The SQLite databases in `data/` are created and seeded automatically on first boot.
Delete the `data/` folder to start from a clean state.

## Ground rules

- **English everywhere in the repo.** README, docs, commit messages and code comments are
  written in English. Strings shown to end users (widget UI, panel labels, GHQ questions)
  stay in Persian — that is the product language.
- **Do not commit secrets.** `.env`, `data/*.db` and `*.xlsx` are git-ignored; never add
  real API keys, passwords or customer data.
- **Keep tenant isolation intact.** Anything you add must be scoped by tenant (API key,
  cache key or database query) — never introduce global mutable state shared across tenants.
- **No external AI service calls.** Matching runs locally by design.

## Making a change

1. Fork the repository and create a branch: `git checkout -b feature/short-description`
2. Make your change and keep it small and reviewable.
3. Verify the app still boots and the core flow works:

   ```bash
   curl http://127.0.0.1:8000/api/chat/health          # {"status":"ok"}
   ```

   Then walk through `/widget?key=nk_…` and `/admin` manually.
4. Commit with a clear English message.
5. Open a pull request describing **what** changed and **why**.

## Reporting bugs

Open an issue with:

- What you expected and what happened
- Steps to reproduce
- Python version and OS

## Project layout

See the [Architecture](README.md#-architecture) section of the README for the full file map.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
