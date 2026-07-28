# Contributing

Clawdence is pre-alpha and being built step by step against a staged plan that is not yet
published. Before starting anything substantial, **open an issue first** — work that arrives
ahead of the step that owns it tends to be rewritten by that step, and that is a waste of your
time rather than a judgement on the code.

## Setup

```sh
uv sync                      # toolchain + pinned Python
uv run pre-commit install    # the hooks are not optional
```

Or `make setup`, which does both.

## Before you push

```sh
make check    # ruff, ruff format --check, mypy --strict, pytest
```

CI runs the same commands plus a full-history secret scan. If `make check` passes locally, CI
should pass too — if it doesn't, that gap is a bug in the setup and worth reporting.

## Secrets

Secret scanning runs at commit time via [gitleaks](https://github.com/gitleaks/gitleaks) and
again in CI over the whole history. Two rules:

1. **Never use `git commit --no-verify`.** It bypasses the only gate that catches a pasted token
   before it becomes permanent.
2. **If a secret does land in a commit, rotate it first.** Rewriting history is the second step,
   not the first — assume anything pushed is compromised.

Config files are gitignored by default (not just `.env`), because config is the likelier leak.
If you need to commit an example, name it `*.template` or `*.example` and put placeholders in it.

Adding an allowlist entry to [`.gitleaks.toml`](.gitleaks.toml) requires a comment explaining why
the match is provably not a secret. "It's noisy" is not a reason.

## Dependencies

Every dependency is pinned to an exact version and resolved through `uv.lock`. CI runs with
`UV_FROZEN=1`, so a lockfile that has drifted from `pyproject.toml` fails the build rather than
resolving silently. Add dependencies with `uv add 'name==X.Y.Z'` and commit the lockfile change
with them.

GitHub Actions are pinned by commit SHA with the tag in a trailing comment. Keep both in sync.

## Code style

Enforced, not debated: `ruff format` for layout, `ruff check` for lint, `mypy --strict` for types.
New modules are fully typed — `Any` and `# type: ignore` need a reason in a comment.

Match the surrounding code. Comments explain *why*, not *what*.

## Commits and PRs

- One logical change per commit; a green `make check` at every commit.
- Reference the plan step in the PR description (`S3`, `S4b`, …) so the work is traceable back to
  the plan and the plan can be corrected when reality disagrees with it.
- If a change contradicts an ADR, say so explicitly and propose the ADR amendment in the same PR.
  ADRs are meant to be reversed with evidence, not quietly ignored.

## Conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
