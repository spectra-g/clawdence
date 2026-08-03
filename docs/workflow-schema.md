# Workflow schema versioning

A workflow file declares the format it is written in:

```yaml
schema_version: 1
name: sprint
version: 1.0.0
```

**This build accepts `schema_version: 1`, and nothing else.** Anything higher or
lower is refused before the file is interpreted, with a message saying which
direction the mismatch is in.

## The two versions in the file, which are not the same thing

| | `schema_version` | `version` |
|---|---|---|
| Whose number is it | Clawdence's | yours |
| What it describes | the *format* the file is written in | the *process* the file describes |
| Shape | one integer | semver — `1.4.0` |
| Who bumps it | a Clawdence release | you, when you change your process |
| Where it shows up | rejected at load time if unsupported | recorded on every run, and a `--resume` against a changed one is refused |

A run is pinned to the workflow name and `version` it started with. Resuming
`run.abc` against a file that now says `1.1.0` is refused rather than
half-executed — the remaining stages of a different process are not the
remaining stages of this one.

## The compatibility policy

**`schema_version` bumps when a file that was valid stops meaning what it
meant.** That is the whole rule. Concretely:

| Change | Bumps? |
|---|---|
| A new optional field, with a default that preserves current behaviour | no |
| A new step type | no |
| A new `$stage.facet`, or a new operator in `when:` | no |
| Widening a range or a length limit | no |
| Removing or renaming a field | **yes** |
| Changing what a field means, or its default | **yes** |
| Making an optional field required | **yes** |
| Narrowing a range so files that were valid no longer are | **yes** |

The reason additive changes do not bump is the reason they are safe: the domain
model forbids unknown fields, so an older build handed a file using something it
does not have refuses it by name — `stages.2.type: Input tag 'audit' found using
'type' does not match any of the expected tags` — rather than ignoring the part
it does not understand. A build either understands the whole file or declines
it. There is no partial interpretation, which is the failure mode the version
gate exists to prevent: a stage silently dropped is a workflow that reports
success for work nobody did.

## What happens when the version does not match

Both directions are refused at load time, before any stage runs, and both say
what to do:

```
sprint.yaml:1: declares workflow schema_version 2, which is newer than this
build understands (1)
  hint: upgrade clawdence to run this workflow; see docs/workflow-schema.md
```

```
legacy.yaml:1: declares workflow schema_version 0, which is older than this
build understands (1)
  hint: migrate the file to schema_version 1; docs/workflow-schema.md lists what changed
```

**Nothing is migrated automatically, in either direction.** A file is data a
person owns and reviews; rewriting it on their behalf — or, worse, reading it
*as if* it had been rewritten — would mean the file in the repository and the
process that ran were two different things. Migration is a documented, manual
step, listed below per version.

## Version history

| `schema_version` | Since | What it is |
|---|---|---|
| 1 | first release | Stages with `script`, `agent`, `runner` and `approval` steps; `when:` guards; `${stage.facet.path}` interpolation; `retry`, `timeout_seconds`, `on_error`. Later extended — additively, with no bump — by the `for_each`, `parallel`, `workflow` and `repeat` composition steps and by `sub_workflows:`. |

No migrations exist yet, because no version has been retired yet. When one is,
its row here gains the field-by-field list of what to change.

## Checking a file against this build

```console
$ clawdence workflow validate examples/*.yaml
ok  examples/sprint.yaml  sprint@1.0.0  (5 stages, schema 1)
```

`validate` checks far more than the version: the YAML, the schema, and every
reference in the file — that each `$stage.facet` names a stage declared *earlier*
than the one reading it, that sub-workflow inputs match their declarations, and
that the call graph has no cycles. Everything knowable from the file is checked
before anything runs, and each failure is reported with the file, the line and
the stage.

See also `clawdence workflow graph` to draw the process, and
`clawdence workflow test` to rehearse it against invented results without
calling a model or touching a repository.
