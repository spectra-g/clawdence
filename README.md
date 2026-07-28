# Clawdence

Workflow-driven orchestration for AI coding agents.

**Status: pre-alpha, no runnable pipeline yet.** What exists is the toolchain, CI, secret-scanning
setup, the domain model, and a workflow engine that executes `script` steps. There is no state
store, no runner, and no agent step — so it can run a build, not a sprint. It is public early so
the build is inspectable from the start, not because any of it is ready to use.

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

## The workflow engine

The process is data. [`examples/toy.yaml`](examples/toy.yaml) is a workflow you can run today:

```sh
uv run clawdence run examples/toy.yaml          # a per-stage trace
uv run clawdence run examples/toy.yaml --json   # the full report, every attempt
```

Stages run in order, each with an optional `when` guard, a retry policy, a declared timeout, and an
`on_error` policy of `fail`, `continue` or `skip_rest`. A guard reads a prior stage as
`$stage.json.field` (its output), `$stage.response.field` (what a human submitted), or
`$stage.succeeded` / `.failed` / `.skipped`. The same references interpolate into arguments as
`${stage.json.field}`.

Two things it does deliberately differently from the engines it borrows its shape from:

- **A workflow that will fail fails before it costs anything.** Bad YAML, an unparseable condition,
  a reference to a stage that does not exist or has not run yet, a placeholder that names nothing —
  all rejected at load time, rather than when execution reaches them and the stages ahead have
  already called an LLM.
- **There is no shell.** `command` is argv, values interpolate into a single element, and expanded
  text is never rescanned. A value containing `; rm -rf /` is one argument that contains
  semicolons. Script steps also get a declared environment plus a small allowlist, never the
  control plane's own — which is where the API keys live.

`agent`, `runner` and `approval` steps parse and validate, and refuse to run with an error naming
the work that will implement them.

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

## Security

The system runs model-generated code against real repositories, so the threat model is written
before the machinery it constrains rather than after it. It is in
[`docs/security/threat-model.md`](docs/security/threat-model.md), and it is blunt about how much of
the design is still unbuilt — most controls are specified, not implemented.

**Do not expose this to input from people you do not trust yet.** The two controls that gate that
are the runner's network egress allowlist and the ingress authorization layer, and neither exists.
Report problems via [SECURITY.md](SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `make check` must be green, secret
scanning is not optional, and dependencies are pinned exactly.

## Licence

MIT — see [LICENSE](LICENSE).
