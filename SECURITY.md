# Security policy

## Reporting a vulnerability

Please report privately via GitHub's **private vulnerability reporting**:
Security tab → *Report a vulnerability*. Please do not open a public issue
for a suspected vulnerability.

Include reproduction steps and the affected version/commit where possible.
The extractor runs with an API key scoped to the backend's `ingestion`
group — if you suspect that key's compromise matters, rotate it in the
backoffice immediately and mention it in the report.

## Scope

This repo covers the LLM-based extractor: source fetching/parsing, the LLM
extraction call, validation, and submission to the backend's ingestion API.
`EXTRACTOR_API_KEY` and the LLM endpoint credentials live in the hoster's
env panel and are never committed.

## Dependencies

Third-party dependency alerts come via **Dependabot alerts**; proactive
weekly update PRs are configured in [`.github/dependabot.yml`](.github/dependabot.yml). Code scanning (CodeQL) runs on `main` and pull
requests.