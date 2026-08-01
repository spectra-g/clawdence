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
make check    # ruff, ruff format --check, mypy --strict, pytest, schema drift
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

## Writing an adapter

Everything the system talks to goes through a port in
[`src/clawdence/ports/`](src/clawdence/ports/), and every adapter has to pass the contract suite
in `tests/ports/contract.py`. Subclass the contract for your port, name the subclass `Test…`, and
override the one fixture that builds your adapter:

```python
class TestMyTracker(TrackerContract):
    @pytest.fixture
    def tracker(self) -> TrackerPort:
        return MyTracker(...)
```

```sh
make contract-tests    # every adapter, wherever it lives
```

The contract does not weaken to accommodate a slower implementation. An obligation that holds only
for the fakes is an obligation nothing real meets. If your adapter genuinely cannot satisfy one —
as `NullTracker` cannot, because it stores nothing — say so in its docstring and hold it to
`NullAdapterContract` instead.

## Tests do not touch the network

The suite runs with TCP and DNS blocked. If you need a socket, mark the test
`@pytest.mark.allow_network` and justify it in the docstring — the exceptions are meant to be
countable. Everything else uses a fake from `clawdence.ports`, a fixture repository from
`tests.harness.repos`, or a cassette.

LLM interactions are recorded. Replay is the default and **a cassette miss is an error, never a
live call** — so a changed prompt fails with instructions rather than quietly spending money.
Re-record deliberately:

```sh
make record    # CLAWDENCE_CASSETTE=record; needs credentials, costs money
```

Cassettes are committed and redacted on write. Review the diff before committing one.

## The domain model and `schemas/`

`src/clawdence/domain/` is the single source for both the Python types and the JSON Schema in
`schemas/`. If you change a type, run `make schema` and commit the regenerated files in the same
commit — `make check` and CI both fail when the two have drifted.

Treat these types as the expensive thing to change, because everything is written against them.
A new field is cheap; a renamed or re-shaped one is not. If a change loosens a constraint that a
docstring says is deliberate — argv instead of shell strings, required tree hashes, budgets that
can only abort — say why in the PR. Those are decisions, not defaults.

## Security-relevant changes

[`docs/security/threat-model.md`](docs/security/threat-model.md) names what this system is
defending against and what it has decided not to defend against. If a change touches the runner's
privileges, what reaches the network, what reaches a prompt, who may approve or submit, or what may
be merged, say in the PR which threat it affects.

A change that weakens a stated mitigation is not automatically wrong — but it needs to move the
threat into the accepted-risks table with a reason, in the same PR. Silently eroding a control is
the failure this document exists to prevent.

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
