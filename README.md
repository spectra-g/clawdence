# Clawdence

Workflow-driven orchestration for AI coding agents.

**Status: pre-alpha, no runnable pipeline yet.** What exists is the toolchain, CI, secret-scanning
setup, the domain model, a workflow engine that executes `script` steps, a state store that records
runs so they survive the process, the ports every integration will sit behind — with an in-memory
implementation of each — and a runner that executes a coding agent against a git worktree, either
on this machine or inside an ephemeral container, with the repository's dependencies installed
first against a cache that outlives the worktree, a cap on how many runs happen at once, and a
sweep that reclaims what a crashed control plane left behind. There is no network policy and no
agent step, and nothing yet decides which repository a piece of work belongs to. It can run a
coding agent; it cannot run a sprint. It is public early so the build is inspectable from the
start, not because any of it is ready to use.

## Decisions taken so far

| | |
|---|---|
| Language | Python for the control plane |
| Workflow engine | Written here rather than adopted, borrowing the schema shape of existing engines |
| State | SQLite: state table + audit log, not event sourcing |
| Entry point | `clawdence` — the only supported one; no supported path calls internal modules |
| Distribution | Docker image plus PyPI, provisionally — re-decided once the first milestone lands |
| Contracts | Pydantic types are the single source; the JSON Schema in [`schemas/`](schemas/) is generated from them |
| Integrations | Everything external sits behind a port, and every adapter passes one shared contract suite |

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
what they are still missing. The runner step is the interesting one of the three: the runner itself
is built, and what has not been built is the part that decides which repository, worktree and
branch to point it at.

## The state store

Runs are recorded in SQLite, so killing the process does not lose the work it had already done:

```sh
uv run clawdence run examples/toy.yaml            # recorded in ~/.clawdence/state.db
uv run clawdence run examples/toy.yaml --no-state # execute and record nothing
uv run clawdence runs list                        # what has run
uv run clawdence runs show RUN_ID                 # one run, step by step
uv run clawdence run WORKFLOW --resume RUN_ID     # continue where it stopped
uv run clawdence runs recover                     # time out abandoned steps, halt stalled runs
```

The `runs` and `steps` tables are the source of truth; a separate append-only `audit` table records
what happened and is explicitly *not* what state is rebuilt from. That choice is what keeps
crash-resume a `SELECT`, idempotency a unique constraint, and a schema change an ordinary migration
rather than a migration of history.

Three properties worth naming:

- **Resume re-runs anything that did not succeed.** A stage that finished is trusted; a stage that
  failed, was skipped, or was still running when the process died is not — so a resumed run
  re-evaluates guards against current results rather than inheriting decisions made before the
  thing that went wrong.
- **A step row is written before the step runs.** That row, with the timeout it was started under,
  is what lets a watchdog find work whose process is gone — the case an executor cannot handle,
  because it is the executor that died.
- **Audit payloads are metadata, not content.** Identifiers, statuses, and error *kinds* — never
  step output or a stderr tail. Redaction at write time is not built yet, and an append-only table
  cannot un-write a pasted key, so what is not yet screened is also not yet carried.

## The ports

Everything the system talks to — chat, issue trackers, GitHub, the runner, memory, the secret
store — is behind an interface in [`src/clawdence/ports/`](src/clawdence/ports/), and each one
ships with an in-memory implementation next to it. v1 had GitHub's API shape, Slack's message
format and Jira's transition ids spread through a 5,107-line orchestrator, which is why it could
not be tested without credentials or run without all three services.

The interfaces are the smaller half. The useful half is `tests/ports/contract.py`: one suite
stating what *every* adapter must do, which each adapter is held to by subclassing it. An
interface says a method exists; it says nothing about whether calling it twice makes two tickets,
and that is the question these ports each get wrong differently. Four rules are stated once
instead of per integration:

- **Retryability travels with the failure.** An adapter raises `TransientError` or
  `PermanentError`; nothing at the call site inspects a message to guess. v1 decided this per
  caller, and in three places by matching on substrings.
- **Every write is idempotent on a key the caller derives.** Notifications, tickets, pull requests
  and runner dispatches key on something stable, so redelivery collides instead of duplicating.
- **"Non-fatal" is a wrapper, not a convention.** `Outbox` is the one implementation of "the
  tracker being down does not fail the run" — bounded, retrying transient failures only, with no
  head-of-line blocking.
- **Merging states what you verified.** `VcsPort.merge` requires the head and base commits the
  evidence was produced against and refuses if either moved. A required check is one a caller has
  to have looked at its evidence to satisfy; an optional one gets omitted.

```sh
uv run pytest -m contract   # or: make contract-tests
```

## The runner

[`src/clawdence/runners/`](src/clawdence/runners/) hands a plan and a worktree to a coding agent
CLI and turns what comes back into a result. Two tiers exist:

- **`container`** — the default. An ephemeral container per run with the worktree bind-mounted at
  the same absolute path it has on the host, every capability dropped, a read-only root filesystem,
  a pid and memory ceiling, and no docker socket. One mount goes in, so the other repositories in
  the registry are not permission-checked — they are absent from the filesystem.
- **`host`** — a subprocess on this machine, for local development only. It has no isolation at
  all, and it is never a default.

Each refuses a repository profile that asks for the other rather than quietly substituting, because
a repo configured for `container` running unisolated is not a smaller version of the intended
behaviour. Both are the same runner with four things swapped — what gets spawned, what the agent's
environment starts from, what the tier can report afterwards, and what has to be given back.

The interesting half is the contract around the process, most of which exists because v1 learned
it the hard way:

- **Output streams while it runs.** v1 captured it all and delivered it at the end, so forty
  minutes of an agent going nowhere looked exactly like forty minutes of progress.
- **Fifteen outcomes, not "failed".** Timeout, OOM kill, disk full, non-zero exit, empty diff,
  failing tests, budget exceeded, network denied, blocked, cancelled, startup failure. They are
  handled differently — failing tests are worth another attempt, an agent blocked on a missing
  dependency is worth a human — and a taxonomy with one value cannot express that.
- **Three of them are invisible to the exit status**, which is what everything above is anchored
  on. An agent can exit **0** having emitted a final turn carrying "your credit balance is too
  low"; that is a *false success*, and it is worse than one undifferentiated value, because
  everything downstream is built to trust the one value it is not allowed to doubt. So the
  agent's own event stream is read for the turn it ended on (`provider-error`) and for whether the
  model ever answered at all (`no-model-response`, the rejected credential — which otherwise looks
  identical to a missing image). The third is `dropped-commit`: an agent that edited files and
  never committed them. The runner commits the work anyway so none of it is lost, and reports
  that the agent never claimed it.
- **The result carries artifacts, not a path to go and look at.** Commits ahead of the declared
  base, whether the tree was left dirty, and which paths — collected in the workspace at the moment
  the work is collected, before the container is removed. The control plane decides the outcome
  from the payload rather than from a directory it may no longer be able to reach. Telling the
  agent's mess from the runner's own is why the files the runner installs are recorded byte for
  byte: a repository that keeps its own `AGENTS.md` gets it back untouched, and an agent that
  deliberately edited that file keeps its edit.
- **A run is not write-only.** Output streaming fixed half the problem; the other half is that
  there was no way to say anything to a run in flight, and no way to stop one from outside the
  process that started it. Both now go through a per-run inbox in the state store, which the runner
  polls: a message becomes a file the agent reads on its next turn, and a cancel stops the work on
  either tier. Messages go out priority-first and are delivered **at most once** — an instruction
  followed twice is worse than one that visibly never arrived, so a message the crashed process was
  holding is recorded as failed rather than requeued, while one nobody has seen yet waits for the
  resumed run. The channel into a container is the bind mount and nothing else, because a socket
  would be a second hole in the boundary the tier exists to draw.
- **A run that goes quiet is a different failure from a run that is late.** The watchdog finds work
  whose process is gone; it cannot see a run that is alive, well inside its declared timeout, and
  has emitted nothing for forty-five minutes — which is what a stuck tool call looks like from
  outside, reporting healthy the whole time. A second detector keys on the timestamp of the newest
  thing the run *said*, and its recovery is to ask the run to stop rather than to mark the row
  dead: the process holding the worktree is the only thing that can collect what the agent
  committed before it hung, so the silent run leaves through the same door a cancel does.
- **Budgets abort mid-run.** Tokens are counted off the stream as they are reported and the
  process is killed when the cap is passed. A dollar cap with no configured prices is refused at
  dispatch rather than accepted and ignored.
- **The worktree is treated as output, not as a workspace.** The diff is re-derived with `git`
  rather than taken from the agent's word, the verdict file is size-capped and never followed
  through a symlink, git is invoked with the config knobs that execute programs pinned off, and
  everything the runner installs — plan, verdict, conventions file — is excluded from git and put
  back afterwards, so none of it can reach a pull request even at a path the repository already
  tracks, where an exclude file has no effect.
- **No control-plane credential is in the environment to steal.** The child's environment is built
  from an allowlist, and the runner refuses to start if a chat, tracker or VCS credential is in it.
  A test asserts that from inside a running agent — and on the container tier, from inside a real
  container. A credential the runner *is* meant to have reaches it by name rather than by value,
  so it never appears in a command line.
- **Runner images are pinned by digest.** A tag is a mutable pointer, and resolving one at dispatch
  means executing whatever was pushed over it since the last run. Refused unless explicitly opted
  out of.

The claims that are only meaningful from inside a container — capabilities, the read-only root, the
memory cap, the absent sibling repository — are checked against a real daemon by `make docker-tests`
rather than asserted about a command line. They are opt-in because they need Docker and a network,
and the rest of the suite has neither.

## Development

Requires [uv](https://docs.astral.sh/uv/). It manages the Python version too, so nothing else
needs installing.

```sh
make setup           # sync the toolchain, install the git hooks
make check           # lint + typecheck + test + schema, exactly what CI runs
make contract-tests  # the port contract suite, against every adapter
make docker-tests    # the container tier against a real daemon (needs docker or podman)
make schema          # regenerate schemas/ after changing the domain model
make schema-test     # assert schemas/ is current and the contracts round-trip
make scan            # gitleaks over the working tree and the history
```

The suite runs with **TCP and DNS blocked** and no LLM calls: fakes and recorded interactions
throughout, with a cassette miss failing loudly rather than reaching a provider. A test that needs
a socket marks itself and says why — the exceptions are meant to be countable.

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
