# Clawdence

Workflow-driven orchestration for AI coding agents.

**Status: pre-alpha, scaffold only.** There is no application code yet — this repository currently
holds the toolchain, CI, and secret-scanning setup the rest of the work will be built on. It is
public early so the build is inspectable from the start, not because any of it is ready to use.

## Decisions taken so far

| | |
|---|---|
| Language | Python for the control plane |
| Workflow engine | Written here rather than adopted, borrowing the schema shape of existing engines |
| State | State table + audit log, not event sourcing |
| Entry point | `clawdence` — the only supported one; no supported path calls internal modules |
| Distribution | Docker image plus PyPI, provisionally — re-decided once the first milestone lands |

Each of these is recorded as an ADR with its cost and the evidence that would reverse it. The
design corpus is not published yet; it is reassessed at the first public milestone. Ask if you
want the reasoning behind any of the above.

## Development

Requires [uv](https://docs.astral.sh/uv/). It manages the Python version too, so nothing else
needs installing.

```sh
make setup      # sync the toolchain, install the git hooks
make check      # lint + typecheck + test, exactly what CI runs
make scan       # gitleaks over the working tree and the history
```

`make setup` installs the pre-commit hooks. **Do not skip it** — secret scanning is a commit-time
gate, and `--no-verify` bypasses it.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `make check` must be green, secret
scanning is not optional, and dependencies are pinned exactly.

## Licence

MIT — see [LICENSE](LICENSE).
