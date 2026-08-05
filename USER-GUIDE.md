# Clawdence — a walkthrough

From scratch: wire up one real repository, submit a real request, watch it
become a pull request. It assumes what `README.md` states — read that first if
any of the shape here is surprising, since this document does not re-derive it.

**Point this at a repository you own and trust.** Ingress authorization and the
runner's network egress allowlist don't exist yet (S10b, S7b — both M3). Public
issues, a repo you don't control, or anyone else's untrusted input are out of
scope until those land.

## 0. Your two credentials, and what each one drives

Clawdence calls a model in two separate places, and this is the thing most
likely to trip you up mid-walkthrough:

- **Agent steps** (`type: agent` — the business analyst, tech lead, architect,
  reviewer roles) run *in the control plane*, through `ports.model.ModelPort`.
  The only adapter today is `AnthropicModels`, hardcoded to Anthropic's Messages
  API (`x-api-key`, `/v1/messages`) — not the OpenAI-compatible shape OpenRouter
  speaks. **An OpenRouter key will not work here.** You need
  `ANTHROPIC_API_KEY`, or a workflow with no agent steps.
- **The runner** (`type: runner` — the thing that actually edits the repository)
  runs *in the data plane*, as a CLI you configure: `codex exec`, `claude-code`,
  whatever `runner.argv` points at. This is where a Codex subscription lives.

| You have | Use it for | How |
|---|---|---|
| Anthropic API key | Agent steps (`sprint.yaml`'s planning stages) | `export ANTHROPIC_API_KEY=...` |
| OpenRouter key | Nothing yet | No adapter exists. Skip agent steps (`quick-fix.yaml`) or write one — `ModelPort` is a `Protocol`, a real extension point |
| Codex subscription (`codex login`, no API key) | The runner | Only on `runner.tier: host` — see below |
| An OpenAI API key | The runner | Either tier, via `runner.secret_env` |

**Why a subscription is tier-locked.** The `container` tier — the default, and
the recommended one — gives the runner a *fresh* `HOME` inside the worktree on
purpose: that's what keeps a container from seeing anything on your machine. A
subscription is file-based auth in `~/.codex`, which lives in your real `HOME`,
and no mount carries it in. The `host` tier inherits your `HOME`, so the login
is just there. So: subscription and no API key means `runner.tier: host`, which
means the coding agent runs unsandboxed on your machine — fine for your own
repo, worth revisiting before anything else. An OpenAI API key works on either
tier via `runner.secret_env`.

**For this walkthrough**, with a Codex subscription and no Anthropic key:
`runner.tier: host` and `quick-fix.yaml`, which has no agent steps.
`sprint.yaml` becomes available the moment `ANTHROPIC_API_KEY` is set.

## 1. Install and sanity-check

`clawdence` is a real console script (`pyproject.toml`'s `[project.scripts]`),
not something that only runs through `uv run`. Install it editably so edits to
this checkout take effect immediately — the rest of this walkthrough drops the
`uv run` prefix on that assumption, though every command also works prefixed
with it from here.

```sh
uv tool install --editable .
clawdence --version
clawdence run examples/toy.yaml     # no config needed — proves the engine runs at all
```

## 2. Pick a repository, and get its profile

Clone (or already have) the repository you're going to let this touch, then ask
the probe to read it:

```sh
mkdir -p ~/.clawdence/repos-profiles
clawdence probe ~/code/my-project --out ~/.clawdence/repos-profiles/my-project.json
```

This prints a report and writes the profile — build system, test command,
whether the tests need Docker, a proposed isolation tier. **Read the report.**
Anything marked `!` is a decision the probe declined to make; the important one
is whether the tests need a Docker socket, a real security boundary and not a
"yes twice and move on".

Then fill in two arrays the probe stubs out but cannot guess, since routing
depends on them:

```json
{
  "routing": {
    "aliases": ["my-project", "myproj"],
    "keywords": ["billing", "invoices"]
  }
}
```

`aliases` is what a request must *name* to win a routing tie; `keywords` is what
it must be *about*. With one repository configured they don't matter — routing
has one answer regardless — but you'll want them the moment you add a second.

## 3. Write the deployment config

The file S11 built. Lives at `$CLAWDENCE_HOME/config.yaml`, default
`~/.clawdence/config.yaml`.

```yaml
# ~/.clawdence/config.yaml
schema_version: 1

paths:
  repo_store: ~/.clawdence/repos      # bare mirrors — separate from your checkout
  work_root:  ~/.clawdence/work       # one worktree per run
  workflows:  /Users/girish/IdeaProjects/clawdence-v2/examples   # the shipped workflows, for now

forge_token_env: GITHUB_TOKEN         # a *name* — the value stays in your shell, never in this file

runner:
  tier: host                         # see §0 — `container` once you have an OpenAI API key
  argv: [codex, exec]                # Clawdence supplies Codex's non-interactive sandbox flags
  conventions_filename: AGENTS.md    # codex's name for the repo-conventions file; CLAUDE.md for claude-code

repos:
  - ~/.clawdence/repos-profiles/my-project.json
```

Before you fill in your own values:

- **Every path resolves relative to this file**, not your shell's cwd. `~`
  expands.
- **`forge_token_env` is a name, never a token.** For a private GitHub repo,
  `export GITHUB_TOKEN=ghp_...` and this file just says which variable to read.
  Public repo over `https`, or anything over `ssh`: omit it
  (`forge_token_env: null`).
- **SSH remotes are non-interactive.** Clawdence passes only `SSH_AUTH_SOCK` to
  Git and forces OpenSSH batch mode, so it never asks for a passphrase mid-run.
  Load the identity first (`ssh-add ~/.ssh/id_ed25519_spectra`, then check
  `ssh-add -l`), or use HTTPS with `forge_token_env`. A missing identity fails
  during the initial fetch, before the coding agent runs.
- **No `runner:` section is a legitimate, if inert, configuration.** `work`
  refuses immediately — before a worktree is acquired — for any request routing
  to a workflow with a `runner` step, naming the missing config rather than
  silently doing nothing. Worth trying once, to see the shape of that refusal.
- If your repo requires signed commits, stop here — `clawdence repos check` will
  tell you plainly rather than let you find out at merge time.

## 4. Check the deployment before spending anything

```sh
clawdence repos list      # what's configured, and whether a runner is wired
clawdence repos show my-project   # the id is the derived repo name — check the JSON's "id"
clawdence repos check     # asks the forge whether the repository can be worked on
```

`repos check` is the "fail before you pay for anything" step S15 couldn't write
until this config file existed. Signed commits, or anything else this system
can't honor, surface here for free.

## 5. Submit a request

```sh
clawdence submit --ref fix-1 --text \
  "The invoice export drops the tax line when a refund is partial."
clawdence inbox list      # confirm it's sitting there, pending
```

`--ref` is yours to choose — it's the idempotency key. The same `--ref` twice is
the same request said twice, not two work items.

## 6. See what would happen — before it happens

```sh
clawdence triage fix-1
```

Read-only: the classified type, the workflow it would route to, the repository
it would land in, and *why* — including every repository it scored against, if
you have more than one. Nothing runs. A wrong repository choice is an
`aliases`/`keywords` edit in the profile, not a code change.

## 7. Run it

```sh
clawdence work fix-1
```

This one spends money and touches your repository: routes, checks out a
worktree, runs the workflow, and — if the runner produced a commit that passes
the diff audit — pushes a branch and opens a pull request. The output tells you
which of those happened, and why if one didn't.

To drain the queue instead of naming one request:

```sh
clawdence work --limit 5     # default is 1, on purpose: each is a real run against a real model
```

`--dry-run` on `work` is identical to `triage` — same read-only preview, spelled
the way you're already typing.

## 7b. Writing your own workflow

The shipped workflows are examples, not a menu. Three commands answer "is this
file right" without spending anything:

```sh
clawdence workflow validate my-workflow.yaml   # will it load, and if not, where
clawdence workflow graph my-workflow.yaml      # what process does it describe
clawdence workflow test my-workflow.yaml       # walk it end to end, running nothing
```

`validate` checks the YAML, the schema, and every `$stage.facet` reference —
including that each names a stage declared *earlier* than the one reading it,
the mistake that otherwise becomes a guard that silently never fires. Failures
name the file, line and stage:

```
my-workflow.yaml:42: stage 'review': 'when' condition '$cod.succeeded' refers to
stage 'cod', which no stage declares (and no scope variable provides)
  hint: values available here: assess, code, plan, request
```

`graph` prints the outline — order, nesting, guards, and what each stage reads.
`--format mermaid` gives a diagram that renders in a pull request.

`test` runs the real engine with every step type stubbed: no model, no
repository, nothing recorded. Because a stubbed step produces nothing for the
next guard to read, it *invents* a result per stage from what the rest of the
file reads out of it — enough to satisfy the guards' comparisons, so the run
takes the happy path rather than skipping everything. It prints what it made up,
so you can tell decisions from placeholders.

To walk a different branch, override a stage's invented result:

```sh
clawdence workflow test examples/sprint.yaml \
  --request-text "add a health endpoint" \
  --output 'assessment={"result": {"size": "L"}}'
```

`schema_version` and what changes bump it: `docs/workflow-schema.md`.

## 7c. Fan-out, parallelism, sub-workflows and loops

Composition (S3b) uses the same ordered stages as its building block, and
`examples/composition.yaml` is an executable one: three runtime-discovered
items, `max_parallel: 2`, per-repository serialization, and a join. It needs no
config and no credentials — every stage is a `script` step:

```sh
clawdence workflow graph examples/composition.yaml   # see the nesting and the cap
clawdence workflow test examples/composition.yaml    # rehearse it; the invented array is printed
clawdence run examples/composition.yaml              # actually run it
```

The live run labels each child by scope — `build[0] / build-item` — so you can
see the fan-out interleave. Four primitives are available:

- **`for_each`** reads a JSON array produced at runtime, runs its nested stages
  with `item` and `index`, and enforces `max_parallel`. An optional `serial_key`
  (normally the item's repository id) stops equal keys overlapping *without*
  consuming a global slot while they wait. Arrays over 10,000 items are refused
  before any child task is allocated.
- **`parallel`** runs named static branches behind the same kind of cap.
- **`workflow`** calls a reusable definition embedded in the same versioned
  document. Inputs are explicit, and the whole call graph is cycle-checked at
  load time — `workflow validate` is where you find that out.
- **`repeat`** exposes the one-based `iteration` and the `previous` result,
  evaluates `until` after each pass, and fails when `max_iterations` is
  exhausted. There is no force-proceed setting to turn the bound into decoration.

A composition stage is itself the join: the stage after it cannot begin until
every child has settled. Each nested execution is a durable row, so resume
applies per item — kill the process mid-fan-out, and resuming trusts only
completed children and reruns the rest.

```sh
clawdence runs show RUN_ID     # the recorded trace
```

One wart worth knowing: `runs show` prints each child's opaque execution id
rather than the readable scope the live run prints. The rows are all there; the
labels are the collision-safe ids.

## 7d. What the runner has to prove

A runner step carries a verification contract — one of `outside-in-tdd`,
`test-after`, `build-only`, `none`. `work` uses `test-after` today; choosing per
repository or per workflow isn't wired yet, and neither is the re-check that
re-derives evidence after a rebase. What you *can* observe on a real run:

- **A claim with nothing behind it is not a claim.** Under `test-after` the
  agent must return a verdict with test counts. Saying "passed" with no evidence,
  or with counts that contradict the claim, is treated the same as a failure and
  is worth another attempt — so the step retries rather than publishing.
  `clawdence runs show RUN_ID` shows the attempts and which band each ended in.
- **Evidence names a commit tree by hash.** Amend, squash, rebase or force-push
  and it stops counting, because the check is string equality rather than a list
  of events somebody remembered to enumerate.
- **Only the assertion reaches the model.** A failing suite emits thousands of
  lines; forwarding them exhausts the step's context budget and truncating them
  drops the assertion. What survives the parse is the test, file, line, message
  and three frames — vendor frames dropped *before* that cut.

That last one depends on a field the probe fills in from your build system:

```sh
grep test_reporter ~/.clawdence/repos-profiles/my-project.json
```

Maven and Gradle get `junit-xml` for free. A `"none"` here — which is what a
plain `pytest` or `go test` setup gets — means there is no structured report to
parse, so failures come back as raw text. Adding the reporter your stack needs
(`pytest-json-report`, `--json`, `go test -json`) and setting the field is the
difference between a retry that sees the error and one that doesn't.

## 8. What "it worked" looks like

- A branch under `clawdence/` on your remote.
- A pull request naming the work item, the workflow and the run id — review it
  like any other proposal; nothing here merges itself.
- `clawdence runs show RUN_ID` for the step-by-step trace.

**What "nothing happened" can mean**, and it's not always a failure:

- **No pull request, no error** — the agent concluded there was nothing to
  change. A legitimate outcome (`RunnerOutcome.EMPTY_DIFF`), not a bug. Check
  `clawdence runs show RUN_ID` for what it looked at.
- **Refused before a worktree was touched** — repository policy, an unresolvable
  route, a missing runner config. The message names which. Nothing was checked
  out, nothing spent.
- **Refused after the runner ran, before publishing** — the diff audit caught
  something (a symlink, a vendored directory, an oversized file). The work exists
  locally; it wasn't pushed. This is the one worth inspecting by hand:
  `clawdence runs show RUN_ID` for the findings.
- **Publication queued for retry** — the runner finished and its commit is
  preserved, but Git or GitHub couldn't finish the branch/push/PR sequence. Do
  not resubmit. Transient failures retry with backoff on a later
  `clawdence work`; permanent or exhausted ones are parked. Inspect with
  `clawdence effects show EFFECT_ID`, fix the cause, then
  `clawdence effects retry EFFECT_ID`. Due effects drain before a fresh agent is
  dispatched, so this doesn't redo the coding work.
- **Still `pending` in `clawdence inbox list`** — it couldn't be routed at all.
  It is *not* silently dropped; it waits until you fix the routing (usually
  `aliases`/`keywords`) and run `work` again.

Publication is the first handler on the generic durable-effects facility. Use
`clawdence effects list --status parked` for the operator queue, and
`clawdence runs show RUN_ID` to see workflow execution and external delivery as
separate statuses. Tracker and notification handlers will reuse the lifecycle
when their owning steps arrive; they don't get private retry tables.

## 9. Back up and restore the state record

Take backups through Clawdence rather than copying a live `state.db`:

```sh
clawdence state backup ~/backups/clawdence-$(date +%F).db
```

SQLite may hold committed pages in a WAL beside the main file, so this uses the
online backup operation to include them. It integrity-checks source and copy,
and refuses to overwrite an existing backup.

Exercise recovery into a path that does not exist:

```sh
clawdence state restore ~/backups/clawdence-2026-08-01.db \
  --state /tmp/clawdence-restore/state.db
clawdence runs list --state /tmp/clawdence-restore/state.db
clawdence effects list --state /tmp/clawdence-restore/state.db
```

Restore requires exactly the schema version this build understands and refuses
an existing destination. To replace production state: verify the clean restore,
stop every Clawdence process, then move files by your normal recoverable
operator procedure. The command will not silently overwrite the system of record.

Known credential shapes, and values under credential-named fields, are replaced
with `[redacted]` before state is written. If a new key shape gets through, put
the exact leaked value in a permission-restricted file and use the audited
escape hatch:

```sh
chmod 600 /tmp/missed-secret
clawdence state redact --secret-file /tmp/missed-secret \
  --reason "provider introduced a key shape the redactor did not know"
```

The secret stays out of the command line, shell history, output and audit event.
The operation rewrites exact matches in content-bearing columns and appends a
tombstone recording operator, reason and counts. Delete the temporary file
through your normal secure procedure afterwards.

## Troubleshooting the two credentials

- **"`agent` steps ... but no model provider was wired"** — you ran `sprint.yaml`
  (or anything with an agent step) without `ANTHROPIC_API_KEY`. Export it, or use
  `quick-fix.yaml`.
- **codex can't find your login inside the run** — you're on
  `runner.tier: container` with a subscription login. Switch to `host` (§0), or
  get an API key and use `runner.secret_env`.
- **"no default image" / "not a workflow name" / anything from `ConfigError`** —
  config-file problems, and the message names the exact field. `clawdence repos
  list` usually surfaces the same thing without spending a run.

## Where to look for more

- `README.md` — the fuller tour of every layer, in the order they were built.
- `docs/workflow-schema.md` — what `schema_version` means, and the compatibility
  policy: which changes bump it, which don't, and what happens when a file and a
  build disagree.
- `docs/security/threat-model.md` — what's built, designed, accepted, and why.
  Worth reading before pointing this at anything you didn't write yourself.
- `clawdence <command> --help` — every flag this guide didn't mention.
