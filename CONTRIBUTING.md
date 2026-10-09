# Contributing to Hopla

## Commands

Needs uv 0.12.x (pinned in `pyproject.toml` under `[tool.uv]`); uv installs Python 3.14 from `.python-version`.

```bash
uv sync --locked                    # install exactly what uv.lock pins
uv run pytest -m "unit or contract" # what CI runs (tests can connect to loopback only)
uv run ruff check .                 # lint (incl. a direct-import denylist for hopla.core)
uv run ruff format .                # format
uv run mypy                         # type check (strict for hopla.core)
uv run hopla --help                 # the command line
```

## Rules

- **Never contact a source site from tests or CI.** Tests use recorded, scrubbed fixtures in `tests/fixtures/`. Raw recon captures stay in the local, git-ignored `recon/` folder.
- **Never get around access controls.** A CAPTCHA, login, challenge page or bot block switches the source off; there are no workarounds (no proxies, VPNs or browser disguises).
- **Be polite:** an identifiable User-Agent, at most one request every 2 seconds per host, and `robots.txt` respected.
- **No secrets in the repo.** Credentials live in a local `.env` (git-ignored) and in GitHub Actions secrets.
- **One task = one branch = one pull request.** Use conventional commits (`feat(scrapers): …`, `fix(core): …`, `test: …`).
- **The core stays pure:** `hopla.core` doesn't import network, database or framework code.
- **Fixtures are minimal:** keep only the rows a test needs, scrubbed of cookies, tokens and session IDs.
