# Security

## Status

**This is pre-alpha software with no isolation built yet.** It ships a domain model and a CLI —
there is no workflow engine, no runner, and no ingestion. Do not run it against anything you care
about, and do not expose it to input from people you do not trust.

The [threat model](docs/security/threat-model.md) is the substantive document: what can go wrong,
what stops it, what is built today, and what has been accepted rather than mitigated. It is
deliberately blunt about how much of the design is still unimplemented.

## Supported versions

None. There is no release, so there is nothing to backport to. Security fixes land on `main`.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository: **Security → Report a
vulnerability**. That keeps the report private until a fix exists.

Please do not open a public issue for a security problem.

There is no bounty, and this is maintained by one person alongside a full-time job — expect a
first response in days rather than hours. If a report has had no acknowledgement after two weeks,
opening a public issue that says only *"awaiting a response on a private report"*, with no detail,
is a reasonable next step.

## What counts as a vulnerability here

Given what the system does, some things that look alarming are the documented design and some
things that look minor are not.

**In scope, and worth reporting:**

- Anything that lets the data plane reach a control-plane credential.
- Anything that lets one run reach another run's worktree, or a repository it was not given.
- Anything that lets request text or repository content select the workflow, the repository, or
  the isolation tier.
- Anything that gets code merged without valid verification evidence for the tree being merged.
- A gap the threat model does not name. That gap is itself the finding.

**Out of scope, because the threat model already accepts it:**

- The model writing incorrect or subtly wrong code into a diff it was authorised to write. Human
  review of the pull request is the control, by design.
- Destruction confined to a run's own ephemeral worktree.
- Anything reachable only by enabling the documented `unrestricted` egress escape hatch, or by
  opting a repository into Docker socket mode. Both remove protections on purpose and say so.

See §6 of the [threat model](docs/security/threat-model.md) for the full list of accepted risks and
what would change each one.
