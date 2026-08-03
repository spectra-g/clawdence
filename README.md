# Clawdence

Workflow-driven orchestration for AI coding agents.

**Status: pre-alpha. The path runs end to end for the first time.** A request submitted at the
command line is now routed to a workflow and a repository, executed — agents in the control plane,
repository code in an ephemeral container — and published as a pull request. Workflows can also
fan out over runtime-produced items, bound how many branches run together, and join them again:

```sh
clawdence submit --text "The billing export drops the tax line on refunds"
clawdence work
```

What exists is the toolchain, CI and secret-scanning setup, the domain model, a workflow engine that
executes `script`, `agent` and `runner` steps, a state store that records runs so they survive the
process, the ports every integration will sit behind — with an in-memory implementation of each and
real adapters for a model provider and for GitHub — a runner that executes a coding agent against a
git worktree with the repository's dependencies installed first against a cache that outlives the
worktree, a cap on how many runs happen at once, a sweep that reclaims what a crashed control plane
left behind, the version-control layer that hands a run its checkout and turns what comes back into
a pull request, and the triage layer that decides which repository and which process a request
belongs to.

**It is still not ready to use, and two absences are the reason.** There is no network policy on the
runner, and no authorization on the way in — so it must not be pointed at input from people you do
not trust. Memory, verification contracts, human approval gates and the observability surface are
all designed and unbuilt. It is public early so the build is inspectable from the start.

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

Composition uses those same ordered stages as its building block:

- `for_each` reads a JSON array produced at runtime, runs its nested stages with `item` and `index`
  values, and enforces `max_parallel`. An optional `serial_key`—normally the item's repository
  id—prevents equal keys from overlapping without consuming a global slot while they wait. Arrays
  above 10,000 items are refused before child tasks are allocated.
- `parallel` runs named static branches behind the same kind of cap. A composition stage is itself
  the join/barrier, so the following stage cannot begin until every branch has settled.
- `workflow` calls a reusable definition embedded in the same versioned YAML document. Inputs are
  explicit, and the complete call graph is checked for cycles while loading.
- `repeat` exposes the one-based `iteration` and the `previous` iteration's result, evaluates
  `until` after each pass, and fails when `max_iterations` is exhausted. There is no force-proceed
  setting that can turn the bound into decoration.

[`examples/composition.yaml`](examples/composition.yaml) is an executable three-item fan-out with
`max_parallel: 2`, per-repository serialization, and a join. Nested executions are durable rows:
each has an opaque collision-safe execution id plus its authored id and readable scope. If the
process dies halfway through fan-out, resuming trusts only completed children and reruns the rest.

`clawdence run` executes a workflow file directly, which is the ad-hoc path and the one to reach for
when writing a workflow. `clawdence work` is the other one: it takes a submitted request, routes it,
and runs the workflow that routing chose — see [triage](#triage-and-the-path-from-a-request-to-a-pull-request).

### Writing one

Three commands act on a file and nothing else, because users will write and modify workflows and a
flexibility nobody can exercise is theoretical:

```sh
uv run clawdence workflow validate examples/*.yaml       # will they load, and if not, where
uv run clawdence workflow graph examples/sprint.yaml     # the process, as an outline or mermaid
uv run clawdence workflow test examples/sprint.yaml      # walk it end to end, running nothing
```

- **`validate`** is the load-time check above, addressed to a person: every failure names the file,
  the **line** and the stage. The line comes from a second parse that maps values back to their
  position, keyed by the same document path pydantic reports its own errors with.
- **`graph`** draws what the file declares — order, nesting, guards, and which stages each one reads.
  Nothing is resolved and nothing is run, so it is safe against a file you did not write.
- **`test`** runs the *real* engine with every step type stubbed: no model call, no repository, no
  state. Because a stub produces nothing for the next guard to read, it first **invents** a result
  per stage from what the rest of the file reads out of it — values chosen to satisfy the
  comparisons the guards make, so the rehearsal takes the happy path instead of skipping everything.
  Every invented value is printed. `--output 'stage={...}'` overrides one to walk another branch.

`schema_version` is checked before anything else and a file from another release is refused rather
than half-interpreted; [`docs/workflow-schema.md`](docs/workflow-schema.md) states which changes bump
it and which do not.

`approval` steps parse and validate and then refuse, with an error naming the step that will
implement them. That refusal is deliberate and it is the same one `runner` steps gave until triage
existed to say which repository they were for: a stub returning success would make a workflow look
like it ran, which is the most expensive possible way to be wrong about an orchestrator.

## Agent steps

An `agent` step is a role, a task, a model and a bounded conversation, and every part of that is
declared in the workflow rather than discovered by trial:

```yaml
- id: requirements
  type: agent
  role: business-analyst          # a prompt file, versioned, overridable
  task: 'Work out what is wanted: ${request.json.text}'   # the request, seeded by triage
  response_schema: requirements   # validated, with repair, before anything reads it
  max_turns: 1
  timeout_seconds: 300
  model:
    model: claude-sonnet-5
    fallbacks: [claude-haiku-4-5-20251001]   # tried when the account is out of quota
    requires: [structured_output]            # a model swap fails validation, not at run time
    temperature: 0.0
  budget:
    max_usd: 0.50
```

See [`examples/spike.yaml`](examples/spike.yaml) — two agent steps and no repository — and
[`examples/sprint.yaml`](examples/sprint.yaml), which is the same engine with four agent steps and a
runner after them. Set `ANTHROPIC_API_KEY` and `clawdence run` wires a provider; without one the
step refuses rather than pretending.

- **Prompts are data, and an override is visible as one.** Role prompts live at
  `<role>/<version>.md`; `CLAWDENCE_PROMPT_PATH` adds directories searched first, so tuning the
  business analyst does not mean forking. Every run records which role, which version, and whether
  the text was ours or yours. The shipped prompts name no build tool, test runner or language — a
  test enforces it, because a role prompt that names one is wrong for every repository using
  something else and the model complies anyway.
- **Turns are a budget, not a loop.** A second turn happens for one reason: the response failed its
  schema and there is budget left to say so. The correction names the failing field and supplies no
  content, because a correction that supplies content gets echoed back as a conclusion.
- **Context is bounded and never silently dropped.** The prompt is measured against the model's
  window before it is sent; over budget does what the stage declared — `fail`, `compact` or
  `truncate` — and the run record says which, because a prompt quietly cut by a provider surfaces
  as a model that ignored half its instructions long after it was paid for.
- **Malformed JSON is repaired, and every repair is named.** Fences, prose, reasoning blocks,
  trailing commas, and — only if the step asked — a document truncated mid-write. Single quotes are
  deliberately *not* converted: the fix for `{'a': 1}` corrupts `{"note": "it's fine"}` silently,
  and a wrong value that validated is worse than a parse failure that can be retried.
- **Steps are stateless.** Every attempt is a fresh conversation built from the prompt registry and
  the run record. Nothing carries between attempts and nothing between stages except through the
  store.
- **Output is a proposal.** An agent step is given a model, prompts, schemas and tools, and nothing
  else — no store, no VCS, no filesystem — so its output cannot arrive already applied. The tool
  surface is empty and refuses a declared tool by name: work that reads or runs a repository belongs
  in a `runner` step, which executes in the data plane.
- **No provider SDK.** The adapter is `urllib` in a thread behind a `ModelPort`, so the control
  plane's dependency tree stays two packages wide. Quota exhaustion and rate limiting are different
  failures — the first falls through to a fallback model, the second does not — which is the
  distinction v1 got wrong and retried against a dead billing account.

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
  thing that went wrong. The rule applies independently to every fan-out item, parallel branch,
  sub-workflow stage and loop iteration.
- **A step row is written before the step runs.** That row, with the timeout it was started under,
  is what lets a watchdog find work whose process is gone — the case an executor cannot handle,
  because it is the executor that died.
- **Secrets are screened on the way in.** Credential-shaped values in request text, step results,
  steering, audit payloads, durable effects and provider errors become `[redacted]` before SQLite
  sees them; credential-named fields are masked regardless of shape. Audit payloads remain
  metadata rather than content as a second layer of reduction.

External delivery has its own durable state. Once a run has committed, Clawdence records an
immutable `publish_pull_request` command — pinned repository, branch, hashes, rendered body and
policy — before contacting the forge. A later `work` invocation claims due effects before starting
another agent. Transient failures back off, permanent or exhausted failures park, and expired
claims are recoverable after a crash; adapters still make the remote write idempotent.

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

The model port is the one that breaks the idempotency rule, on purpose, and it argues its case in
its own module docstring: the retry a completion has to support is "that response failed its schema,
ask again", which a port answering from a cache cannot do. Charging twice is prevented one layer up
instead, where the executor already re-runs a stage only if it did not previously succeed.

```sh
uv run pytest -m contract   # or: make contract-tests
```

## The runner

[`src/clawdence/runners/`](src/clawdence/runners/) hands a plan and a worktree to a coding agent
CLI and turns what comes back into a result. Three tiers exist:

- **`container`** — the default. An ephemeral container per run with the worktree bind-mounted at
  the same absolute path it has on the host, every capability dropped, a read-only root filesystem,
  a pid and memory ceiling, and no docker socket. One mount goes in, so the other repositories in
  the registry are not permission-checked — they are absent from the filesystem.
- **`container+docker:socket`** — the same, plus the host daemon's socket, for repositories whose
  integration tests need one (testcontainers). It is the one place here where a control is
  deliberately made defeatable: a process that can reach the daemon can ask it for a container with
  the host's filesystem in it. So it costs four separate acknowledgements — the repository profile
  does not validate without one, the work has to have come from a trusted submitter, a runner has
  to have been built for the tier, and testcontainers' own reaper has to be left on — and the
  socket reaches the agent's container only, never the dependency install. See
  [the threat model](docs/security/threat-model.md#t6--host-escape-via-the-docker-socket--partly-built).
- **`host`** — a subprocess on this machine, for local development only. It has no isolation at
  all, and it is never a default.

Each refuses a repository profile that asks for another rather than quietly substituting, because
a repo configured for `container` running unisolated is not a smaller version of the intended
behaviour — and a repo that needs a daemon, run without one, reports a missing capability as the
agent's failure. All three are the same runner with four things swapped — what gets spawned, what
the agent's environment starts from, what the tier can report afterwards, and what has to be given
back.

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
rather than asserted about a command line. The socket tier needs that even more, because each of
its three constraints fails *silently* when it is wrong: a sibling container whose bind mount does
not resolve gets an empty directory rather than an error, and a test that cannot find the host
hangs rather than fails. They are opt-in because they need Docker and a network, and the rest of
the suite has neither.

## Version control

[`src/clawdence/vcs/`](src/clawdence/vcs/) is where a run gets somewhere to work and where its
output becomes a pull request. One bare mirror per repository holds the objects; a worktree per run
hangs off it.

```
<mirrors>/<repo>-<digest>.git      objects, refs, one lock
<work>/<run-id>/<repo>             a checkout per run
```

- **Partial, never shallow.** `--depth=1` is the usual way to make a large clone fast and it is
  unusable here: a shallow repository has no merge base, so nothing can rebase, ask whether a branch
  is behind, or bind evidence to a tree. `--filter=blob:none` gets the same first-clone win — the
  bytes are in file contents, not in commits — and git fetches what it needs on demand.
- **The remote's refs stay out of `refs/heads`.** A `--mirror` refspec is `+refs/*:refs/*`, so a
  pruning fetch deletes local branches the remote has not seen — which is exactly the state a run is
  in between creating its branch and pushing it.
- **One lock per repository, held by the kernel.** `flock`, not a pid file: the case that matters is
  the control plane being killed, and a flock is released on exit, on crash and on `kill -9`, so
  there is no stale state to reason about. Three concurrent runs on one repository is a test.
- **A branch is a function of the work item.** So a retried run continues its own branch instead of
  opening a second pull request beside it, and editing an issue's title does not split one piece of
  work into two. The name is *built* from a closed `[a-z0-9-]` alphabet rather than sanitised, which
  is the same decision `script` steps make by being argv: remove the grammar and nothing has to be
  escaped.
- **Release deletes a branch only if it never moved.** "A cancelled run leaves no orphaned worktree
  or branch" is the goal, and the naive reading destroys work: between the agent committing and the
  push succeeding, the local branch is the only copy.
- **One diff audit, at the boundary.** v1 defended against the `node_modules` cascade with four
  layers. This has one, and it runs on the diff about to be published: symlinks, submodule pointers,
  vendored directories and oversized files. It reports; the caller refuses.
- **Merging states what was verified.** `merge` requires the head and base hashes the evidence was
  produced against. The base is re-read from the remote every time rather than taken from the pull
  request's own record of it, and the head match is handed to the forge as well, where it is atomic
  with the merge.
- **The credential never enters a command line or a remote URL.** `ps` is public and a cloned URL is
  persistent. The token reaches git through a 0600 config file removed on the way out, scoped to one
  parsed origin. See [the threat model](docs/security/threat-model.md#t24--the-forge-credential--built).

Repositories that require signed commits are refused at configuration time, with the reason: the
runner commits with `--no-gpg-sign` on purpose, and a signing key here would let the process every
model's output passes through mark commits as verified.

## The project probe

[`src/clawdence/probe/`](src/clawdence/probe/) reads a repository and proposes the profile the
runner needs — build system, the three commands, toolchain pins, whether the tests need Docker.
v1 kept all of this in a hand-written registry, where every field was something somebody had to
know and keep current.

```sh
clawdence probe ~/code/acme-billing              # a report, and what still needs a person
clawdence probe ~/code/acme-billing --json       # the profile and the reasoning
clawdence probe ~/code/acme-billing --out repos/acme-billing.json
```

It **proposes**. Nothing is written, registered, or applied; the output is JSON to read and commit,
and the exit status is `1` when something in it still needs a human — the answer a script probing
twenty repositories wants.

Three properties are worth stating, because each is somewhere the obvious version goes wrong:

- **Every field carries the file that justified it**, and every field it *declines* to set says
  what was missing. A proposal with no evidence is unreviewable, and the only question a reviewer
  has is how the probe knows.
- **It leaves a field empty rather than guessing.** An empty `test_command` asks its reviewer a
  question; a plausible wrong one answers it and gets committed. So a `package.json` with no
  `scripts.test` gets nothing rather than `npm test`, a Python repo with no sign of pytest gets
  nothing rather than a command that collects nothing and exits 0, and a Node repo with no lockfile
  gets no install command rather than one that resolves fresh versions on every run.
- **It cannot grant the Docker socket.** `needs_docker` is inferred from *declared* dependencies
  and compose files — not from a `Dockerfile`, which says the project is packaged as an image, and
  not from what is installed under `node_modules`, which is somebody else's manifest. But the tier
  that provides a daemon needs `docker_socket_acknowledged`, and that is a person accepting
  something equivalent to host root. So the probe raises the question, names the evidence, and
  proposes a tier with no daemon in it. Nothing this command emits can be committed straight into a
  run with a socket in it.

The repository is read as untrusted input, because it is: bounded file reads, no walk of the tree,
nothing outside the root followed through a symlink, and `pom.xml` and the Gradle DSL matched as
text rather than handed to an XML parser or executed.

## Ingestion

[`src/clawdence/ingest/`](src/clawdence/ingest/) turns a request into a `WorkItem`, and
[`store/intake.py`](src/clawdence/store/intake.py) keeps it. The command line is the first
`IngestPort` source; Slack and GitHub issues are the same package with a different envelope, and
neither may be enabled before the ingress trust boundary exists.

```sh
clawdence submit --ref REQ-1 --text "Fix the checkout total"   # or --file, or a pipe
clawdence submit --ref REQ-1 --text "Fix the tax line" --amend # updates; does not duplicate
clawdence submit --withdraw REQ-1                              # takes it back
clawdence submit --reply thread-9 --text "Only on CI"          # continues a conversation
clawdence inbox list                                           # what has been submitted
clawdence inbox show REQ-1                                     # verbatim, with its conversation
```

The CLI looks like the easy adapter and is the only one that **cannot cheat**: Slack holds a socket
and GitHub holds a connection, so either could deduplicate in a dictionary and look correct.
`submit` is one process and whatever acts on the request is another, so the guard has to be a
unique constraint in a file. Four properties fall out of that, and each is one the obvious version
gets wrong:

- **A request is not a message.** v1 took Slack messages and every message was new work, so it
  never modelled what a request does over its life. Four verbs here — submit, amend, withdraw,
  reply — because every source does all four. `--ref` is the idempotency key and you own it:
  submitting the same one twice is one request said twice.
- **An amendment is inferred from content, never from a verb the source sent.** A webhook
  redelivery and an edit are the same POST. So an arrival is compared against what is stored, with
  the identity fields excluded — and if the edit lands *after* the pipeline picked the request up,
  it goes back in the queue and says so, because a correction that arrives thirty seconds late is
  still the request.
- **The body is stored byte for byte.** No summarising, no reflowing, no stripping. Repository
  routing reads this field, and v1's rewrite is what dropped the product names from it. A derived
  title is the first line — a selection from the text, not a summary of it.
- **The system will not ingest its own output.** It posts to the channels it reads from, so
  without one reserved identity, refused once at intake, its own summary becomes work that
  produces another summary.

Rate limiting, submitter authorisation and webhook signatures are **not here** — they are the
ingress trust boundary, and the size caps in this package are resource bounds rather than that
control.

## Triage, and the path from a request to a pull request

[`src/clawdence/triage/`](src/clawdence/triage/) is the composition root: it decides which workflow
runs and which repository the work lands in, and then carries the request the rest of the way. Until
it existed every other piece was built and tested and nothing joined them.

A deployment is one file — which repositories exist, where their mirrors go, and what runs the code:

```yaml
# ~/.clawdence/config.yaml
schema_version: 1
paths:
  repo_store: ~/.clawdence/repos     # bare mirrors, one per repository
  work_root:  ~/.clawdence/work      # a worktree per run
  workflows:  ./workflows            # a routed name resolves to <dir>/<name>.yaml
forge_token_env: CLAWDENCE_FORGE_TOKEN   # a *name*; never a token
runner:
  tier: container
  image: ghcr.io/example/runner@sha256:…  # digest-pinned; a tag is refused
  # This process is already inside the runner container's hardened boundary.
  argv: [codex, --dangerously-bypass-approvals-and-sandbox, exec]
  secret_env: {OPENAI_API_KEY: runner-llm-key}   # the runner's own scoped key
repos:
  - repos/acme-billing.json          # written by `clawdence probe --out`
```

```sh
clawdence repos list                 # what this deployment is wired to
clawdence repos check                # can each repository be worked on as configured?
clawdence triage REQ-1               # what *would* happen, in full, changing nothing
clawdence work REQ-1                 # route, run, push, open the pull request
```

The registry is the probe's output read back, so there is no second format to keep in step. `repos
check` is the "fail at configuration time, not at merge time" verb: a repository requiring signed
commits is refused before an agent step, a container or a test suite has been paid for.

- **The two decisions are logged with their reasons, and overridable.** Every routing decision
  records the candidates, their scores and the terms that matched, so "why did this go there" is
  answerable and the fix is an edit to a repository profile. `--workflow` and `--repo` at submit
  time override either.
- **Routing reads the raw request text — and that is safe because the candidate set is closed.**
  v1 routed off a paraphrased title and the paraphrase dropped product names, so a paraphrase must
  not change the answer. But request text is the most attacker-controlled string in the system, so
  it is a *selector over the repositories the operator configured* and never a source of new ones:
  it can reorder them and can never extend them. Ambiguity refuses rather than guessing — a winner
  must beat the runner-up outright — and a tie asks a person. See T25 in the threat model.
- **A workflow sees the request as `${request.json.text}`.** Not a stage: a reserved name, seeded
  by the pipeline, which is what retired the `intake` script step every shipped workflow used to
  open with. A stage *called* `request` is a load error.
- **Three shipped workflows, one engine.** [`sprint`](examples/sprint.yaml) is four agent steps and
  a runner; [`quick-fix`](examples/quick-fix.yaml) is a runner and nothing else, for a one-line bug
  that does not need three models to agree it is a one-line bug; [`spike`](examples/spike.yaml) has
  no runner step at all, so it is never given a checkout and *cannot* open a pull request. None of
  the three is a special case anywhere in the code.
- **Nothing is published that a reviewer cannot read.** The diff is audited before it is pushed —
  symlinks, submodules, vendored directories, huge files — and a run that committed nothing opens
  no pull request rather than an empty one. A run whose *later* stages failed does publish, with
  the failure named in the body: an agent's product is a proposal entering the normal review path,
  and throwing it away would be deciding the review.
- **A forge interruption does not rerun the agent.** The completed commit remains in the mirror and
  its due publication effect is retried before fresh work on the next `clawdence work`. Delivery
  has transactional claims, bounded transient backoff, permanent-failure parking and an explicit
  `clawdence effects retry EFFECT_ID` operator path.
- **A request nobody could route stays in the queue.** Acknowledging it would leave a person
  waiting on work that will never start.

## The dev loop

[`src/clawdence/devloop/`](src/clawdence/devloop/) is the tooling that makes every other step's
verification quick: get back to a clean environment, read the log, and check that the log and the
state agree.

```sh
clawdence reset --dry-run              # what would go
clawdence reset --keep-inbox           # runs, steps, log and debris go; requests stay
clawdence runs replay RUN_ID           # rebuild the run from its log and diff it
clawdence audit RUN_ID                 # the timeline
clawdence audit --dead-letters         # records that could not join it
clawdence runs show RUN_ID             # steps, durations, and why one failed
clawdence state backup ~/backups/clawdence.db
clawdence state restore ~/backups/clawdence.db --state /clean/path/state.db
```

- **A partial reset is the bug, not a smaller reset.** v1's `reset-pipeline.sh` cleared the
  `.jsonl` event files and left `sessions.json` behind, and messages sent to those stale sessions
  were *silently dropped*. In v2 the same shape is an `acknowledged` request whose run has been
  deleted — collected by nothing, re-queued by nothing. So the default clears it, and
  `--keep-inbox` puts anything already picked up **back in the queue** rather than leaving it in a
  state nothing can act on.
- **`reset` is a reaper sweep with every protection switched off.** `reap` reclaims what is
  unclaimed *and* not recent; reset passes an empty live set and zero retentions, which turns both
  questions off. That is why it asks before it acts, refuses while anything is still running, and
  refuses rather than prompts when nothing is on a terminal.
- **Replay compares; it does not restore.** ADR-0005 keeps state out of the log's hands, so the
  fold happens in memory and the deliverable is the diff. What it actually catches is a writer that
  changes state without recording it — the first run it was pointed at found two gaps in the
  ledger's own payloads. Half the step record is unobservable by design (S4 keeps output and
  messages out of the log), and the report names those fields rather than quietly not comparing
  them.
- **Backup is an online SQLite backup, not a copy of `state.db`.** A filesystem copy can omit
  committed pages still in the WAL. Backup and restore check SQLite integrity and require exactly
  this build's schema; restore refuses an existing destination so recovery is exercised into a
  clean environment.
- **A missed secret has an explicit repair path.** Put the exact value in a private file and run
  `clawdence state redact --secret-file PATH --reason TEXT`. The value never enters argv or audit;
  matching content is rewritten to `[redacted]` and a metadata-only tombstone records who repaired
  it, why, and how many rows changed.

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
make record          # re-record the LLM cassettes (costs money; needs credentials)
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
