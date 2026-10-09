# Contributing to Hopla

## Commands

```bash
uv sync --locked          # install exactly what uv.lock pins
uv run pytest             # tests (outbound network is blocked in tests)
uv run ruff check .       # lint
uv run ruff format .      # format
```

## Rules

- **Never contact a source site from tests or CI.** Tests use recorded, scrubbed fixtures in `tests/fixtures/`. Raw recon captures stay in the local, git-ignored `recon/` folder.
- **Never get around access controls.** A CAPTCHA, login, challenge page or bot block switches the source off; there are no workarounds (no proxies, VPNs or browser disguises).
- **Be polite:** an identifiable User-Agent, at most one request every 2 seconds per host, and `robots.txt` respected.
- **No secrets in the repo.** Credentials live in a local `.env` (git-ignored) and in GitHub Actions secrets.
- **One task = one branch = one pull request.** Use conventional commits (`feat(scrapers): …`, `fix(core): …`, `test: …`).
- **The core stays pure:** `hopla.core` doesn't import network, database or framework code.
- **Fixtures are minimal:** keep only the rows a test needs, scrubbed of cookies, tokens and session IDs.
