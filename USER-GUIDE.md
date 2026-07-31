# Clawdence — a walkthrough

This is a from-scratch walkthrough: get one real repository wired up, submit a
real request, and watch it become a pull request. It assumes nothing beyond
what `README.md` already states — read that first if any of the shape here is
surprising, since this document does not re-derive it.

**Point this at a repository you own and trust.** Ingress authorization and the
runner's network egress allowlist don't exist yet (S10b, S7b — both M3). Public
issues, a repo you don't control, or anyone else's untrusted input are all out
of scope until those land.

## 0. Your two credentials, and what each one actually drives

You mentioned an OpenRouter key and a Codex subscription. Before anything
else, here's exactly where each one does and doesn't reach, because this is
the thing most likely to trip you up mid-walkthrough.

Clawdence has two separate places a model gets called:

- **Agent steps** (`type: agent` in a workflow — the business analyst, tech
  lead, architect, reviewer roles) run *in the control plane*, through
  `ports.model.ModelPort`. The only adapter that exists today is
  `AnthropicModels`, and it's hardcoded to Anthropic's own Messages API
  (`x-api-key` header, `/v1/messages`) — not the OpenAI-compatible shape
  OpenRouter speaks. **An OpenRouter key will not work here.** You need
  `ANTHROPIC_API_KEY`, or a workflow with no agent steps.
- **The runner** (`type: runner` — the thing that actually edits the
  repository) runs *in the data plane*, as a CLI you configure —
  `codex exec`, `claude-code`, whatever you point `runner.argv` at. This is
  where your Codex subscription lives.

So, concretely:

| You have | Use it for | How |
|---|---|---|
| Anthropic API key | Agent steps (`sprint.yaml`'s planning stages) | `export ANTHROPIC_API_KEY=...` |
| OpenRouter key | Nothing yet | No adapter exists. Either skip agent steps (`quick-fix.yaml`) or write one — `ModelPort` is a `Protocol`, this is a real extension point |
| Codex subscription (`codex login`, no API key) | The runner | Only on `runner.tier: host` — see below |
| An OpenAI API key | The runner | Either tier, via `runner.secret_env` |

**Why the subscription is tier-locked.** The `container` tier — the default,
and the one the project recommends — gives the runner a *fresh* `HOME`
inside the worktree, on purpose: it's what keeps a container from seeing
anything on your machine. Your `~/.codex` login (a subscription is
file-based auth from `codex login`) lives in your real `HOME` and there is
no mount that carries it in. The `host` tier inherits your actual `HOME`,
so the existing login is just there. Practically: if you only have the
subscription and no API key, set `runner.tier: host`. That means the coding
agent runs unsandboxed on your machine — acceptable for your own repo,
something to revisit before pointing this at anything else.

If you later get an OpenAI API key, `runner.secret_env` works on either
tier and lets you use the safer default.

**For this walkthrough**, since you have the Codex subscription and (for now)
no Anthropic key, the practical path is: `runner.tier: host`, and
`quick-fix.yaml` as the workflow, since it has no agent steps and therefore
needs no Anthropic key at all. `sprint.yaml` becomes available the moment
`ANTHROPIC_API_KEY` is set.

## 1. Install and sanity-check

`clawdence` is a real console script (`pyproject.toml`'s `[project.scripts]`),
not something that only runs through `uv run`. Install it once, editably, so
edits to this checkout take effect immediately and `clawdence` is just a
command on your `PATH` from here on — the rest of this walkthrough drops the
`uv run` prefix on that assumption:

```sh
uv tool install --editable .
clawdence --version
clawdence run examples/toy.yaml     # no config needed — proves the engine runs at all
```

(If you'd rather not touch your `PATH`, every command below also works
prefixed with `uv run` from this checkout — the two are equivalent, this is
purely about typing less.)

## 2. Pick a repository, and get its profile

Clone (or already have) the repository you're going to let this touch. Then
ask the probe to read it:

```sh
mkdir -p ~/.clawdence/repos-profiles
clawdence probe ~/code/my-project --out ~/.clawdence/repos-profiles/my-project.json
```

This prints a report and writes the profile — build system, test command,
whether the tests need Docker, a proposed isolation tier. **Read the report.**
Anything marked as needing you (`!`) is a decision the probe declined to make;
the most important one is whether it thinks the tests need a Docker socket,
which is a real security boundary and not a "yes twice and move on."

Open the written JSON and fill in two empty arrays by hand — the probe
already stubs them out under `routing`, it just doesn't know what to put in
them, and routing depends on them:

```json
{
  "routing": {
    "aliases": ["my-project", "myproj"],
    "keywords": ["billing", "invoices"]
  }
}
```

`aliases` is what a request has to *name* to win a routing tie; `keywords`
is what it has to be *about*. With only one repository configured these
don't matter yet — routing has one answer regardless — but you'll want them
the moment you add a second.

## 3. Write the deployment config

This is the file that used to not exist — the thing S11 built. It lives at
`$CLAWDENCE_HOME/config.yaml`, which defaults to `~/.clawdence/config.yaml`.

```yaml
# ~/.clawdence/config.yaml
schema_version: 1

paths:
  repo_store: ~/.clawdence/repos      # bare mirrors — separate from your checkout
  work_root:  ~/.clawdence/work       # one worktree per run
  workflows:  /Users/girish/IdeaProjects/clawdence-v2/examples   # the shipped workflows, for now

forge_token_env: GITHUB_TOKEN         # a *name* — the value stays in your shell, never in this file

runner:
  tier: host                         # see §0 — set to `container` once you have an OpenAI API key
  argv: [codex, exec, --full-auto]   # your actual codex invocation; check `codex exec --help`
  conventions_filename: AGENTS.md    # codex's name for the repo-conventions file; CLAUDE.md for claude-code

repos:
  - ~/.clawdence/repos-profiles/my-project.json
```

A few things worth knowing about this file before you fill in your own values:

- **Every path resolves relative to this file**, not to wherever your shell
  happens to be. `~` is expanded.
- **`forge_token_env` is a name, never a token.** If your repo is a private
  GitHub repo, `export GITHUB_TOKEN=ghp_...` in your shell and this file
  just says which variable to read. Public repo over `https`, or anything
  over `ssh`: leave this out entirely (`forge_token_env: null`).
- **No `runner:` section is a legitimate, if inert, configuration.** `work`
  refuses immediately — before a worktree is even acquired — for any request
  that would route to a workflow with a `runner` step, naming the missing
  config rather than silently doing nothing or spending a checkout first.
  Worth trying once, to see the shape of that refusal.
- If your repo requires signed commits, stop here — `clawdence repos check`
  (next step) will tell you plainly rather than let you find out at merge
  time.

## 4. Check the deployment before spending anything

```sh
clawdence repos list      # what's configured, and whether a runner is wired
clawdence repos show my-project   # the id is the derived repo name, no prefix — check the JSON's "id"
clawdence repos check     # asks the forge whether the repository can actually be worked on
```

`repos check` is the "fail before you pay for anything" step S15 originally
couldn't write because this config file didn't exist yet. If your repo
requires signed commits or something else this system can't honor, this is
where you find out — for free.

## 5. Submit a request

```sh
clawdence submit --ref fix-1 --text \
  "The invoice export drops the tax line when a refund is partial."
```

`--ref` is yours to choose — it's the idempotency key. Submitting the same
`--ref` twice is the same request said twice, not two work items.

```sh
clawdence inbox list      # confirm it's sitting there, pending
```

## 6. See what would happen — before it happens

```sh
clawdence triage fix-1
```

This is read-only: it shows you the classified type, the workflow it would
route to, the repository it would land in, and *why* — including every
repository it scored against, if you have more than one configured. Nothing
runs. If the repository choice looks wrong, that's an `aliases`/`keywords`
edit in the profile, not a code change.

## 7. Run it

```sh
clawdence work fix-1
```

This is the one that spends money and touches your repository: routes,
checks out a worktree, runs the workflow, and — if the runner produced a
commit that passes the diff audit — pushes a branch and opens a pull
request. Watch the output; it tells you which of these happened and why, if
one of them didn't.

If you'd rather drain the whole queue instead of naming one request:

```sh
clawdence work --limit 5     # takes up to 5 pending requests; default is 1, on purpose
```

The default is 1 because each one is a real run against a real model. `--dry-run`
on `work` is identical to `triage` — same read-only preview, just spelled the
way you're already typing.

## 8. What "it worked" looks like

- A branch under `clawdence/` on your remote.
- A pull request, with a body naming the work item, the workflow, and the run
  id — review it like any other proposal; nothing here merges itself.
- `clawdence runs show RUN_ID` for the step-by-step trace if you want
  to see what the agent actually did.

**What "nothing happened" can mean**, and it's not always a failure:

- **No pull request, no error** — the agent looked at the request and
  concluded there was nothing to change. That's a legitimate outcome, not a
  bug (`RunnerOutcome.EMPTY_DIFF` under the hood).
  Check `clawdence runs show RUN_ID` to see what it actually looked at.
- **Refused before a worktree was touched** — repository policy, an
  unresolvable route, a missing runner config. The message names which.
  Nothing was checked out, nothing was spent.
- **Refused after the runner ran, before publishing** — the diff audit
  caught something (a symlink, a vendored directory, an oversized file). The
  work exists locally; it wasn't pushed. This is the one case worth looking
  at by hand — `clawdence runs show RUN_ID` to see the findings.
- **Request still `pending` in `clawdence inbox list`** — it couldn't be
  routed at all. It's *not* silently dropped; it stays in the queue until
  you fix the routing (usually an `aliases`/`keywords` edit) and run `work`
  again.

## Troubleshooting the two credentials, concretely

- **"`agent` steps ... but no model provider was wired"** — you ran
  `sprint.yaml` (or anything with an agent step) without `ANTHROPIC_API_KEY`
  set. Either export it, or use `quick-fix.yaml`.
- **codex can't find your login inside the run** — you're on
  `runner.tier: container` with a subscription login. Switch to `host` (§0),
  or get an API key and use `runner.secret_env`.
- **"no default image" / "not a workflow name" / anything from `ConfigError`**
  — these are all config-file problems, and the message names the exact
  field. `clawdence repos list` will usually surface the same thing without
  spending a run on it.

## Where to look when you want more than this walkthrough gives

- `README.md` — the fuller tour of every layer, in the order they were built.
- `docs/security/threat-model.md` — what's built, what's designed, what's
  accepted, and why. Worth reading before you point this at anything you
  didn't write yourself.
- `clawdence <command> --help` — every flag this guide didn't mention.
