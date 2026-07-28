# Clawdence

Workflow-driven orchestration for AI coding agents.

**Status: pre-alpha, no runnable pipeline yet.** What exists is the toolchain, CI, secret-scanning
setup, and the domain model — the typed contracts everything else will be written against. There
is no engine, no state store, and no runner. It is public early so the build is inspectable from
the start, not because any of it is ready to use.

## Decisions taken so far

| | |
|---|---|
| Language | Python for the control plane |
| Workflow engine | Written here rather than adopted, borrowing the schema shape of existing engines |
| State | State table + audit log, not event sourcing |
| Entry point | `clawdence` — the only supported one; no supported path calls internal modules |
| Distribution | Docker image plus PyPI, provisionally — re-decided once the first milestone lands |
| Contracts | Pydantic types are the single source; the JSON Schema in [`schemas/`](schemas/) is generated from them |

Each of these is recorded as an ADR with its cost and the evidence that would reverse it. The
design corpus is not published yet; it is reassessed at the first public milestone. Ask if you
want the reasoning behind any of the above.

## The domain model

[`src/clawdence/domain/`](src/clawdence/domain/) holds typed schemas and no behaviour: work items,
workflows and stages, runs and step results, the runner request/result pair, repo profiles,
verification contracts, budgets, and audit events. Each module says why its fields are shaped the
way they are, because several encode decisions that are cheap now and expensive later — script
commands are argv rather than shell strings, verification evidence cannot exist without naming the
tree it was produced against, and exceeding a budget has no configuration under which it continues.

`schemas/` is generated from those types and committed, so a contract change shows up as a diff in
review. CI fails if the two drift.

## Development

Requires [uv](https://docs.astral.sh/uv/). It manages the Python version too, so nothing else
needs installing.

```sh
make setup        # sync the toolchain, install the git hooks
make check        # lint + typecheck + test + schema, exactly what CI runs
make schema       # regenerate schemas/ after changing the domain model
make schema-test  # assert schemas/ is current and the contracts round-trip
make scan         # gitleaks over the working tree and the history
```

`make setup` installs the pre-commit hooks. **Do not skip it** — secret scanning is a commit-time
gate, and `--no-verify` bypasses it.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `make check` must be green, secret
scanning is not optional, and dependencies are pinned exactly.

## Licence

MIT — see [LICENSE](LICENSE).
