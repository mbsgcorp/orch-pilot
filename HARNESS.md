# HARNESS.md: the orchestrator contract for mbsgcorp/orch-pilot

Installed by `orch enroll` at 2026-09-29T14:01:34Z. Change it only by re-running enroll, never in a task branch.

## What this is
This repo takes work as GitHub issues and returns it as pull requests. A human files an issue, an agent
runner builds it on its own branch, two Codex reviews check it, and a human merges or sends it back.
Any harness that speaks this contract can be the runner. Nothing merges without a human.

## The contract
```
issue #42 (label agent-ready)  ->  branch agent/42  ->  PR "agent(#42): <summary>"  ->  human merges
```
One issue, one branch, one PR. The issue body is the spec; the PR body is the evidence.

## Labels
| Label | Meaning |
|---|---|
| `agent-ready` | Go: runner may claim this issue |
| `agent-claimed` | A runner has claimed this issue |
| `agent-building` | Build in progress |
| `agent-review` | Codex review / triage in progress |
| `agent-fix` | Human requests another round on the open PR |
| `agent-blocked` | Escalated: runner stopped, human decision needed |
| `agent-done` | PR ready for human review/merge |
| `risk:low` / `risk:medium` / `risk:high` / `risk:critical` | Risk from the issue form |
| `runner:hermes` | Route to the Hermes-Box runner |

## Example issue body
Filed with the **Agent Task** form. Submitting the form applies `agent-ready`: submitting is "go."
```
### Business Goal
Operators need to see which repos are enrolled without opening each one.
### Acceptance Criteria
1. When `orch list` runs, then it prints one line per file in enrolled/.
### Non-Goals
No network calls.
### Test Plan
python -m unittest discover -s tests -v
### Rollback Plan
Revert the PR.
### Risk
Low
```

## Branch rules
- Work only on `agent/<issue-number>`, e.g. `agent/42`. Never commit to or push `main`.
- Stage explicit paths: `git add orch/list.py tests/test_list.py`. Never `git add -A` or `git add .`.
main is protected for everyone, administrators included; changes land only by merged PR.

## PR body the runner produces
```
## Request            link to the issue, one-line restatement
## What was built     files changed, one sentence each
## Evidence           commands run and their real output
## Review history     each Codex round: severity counts, what was fixed
## Deferred / your call   findings the runner did not act on, and why
## Deploy checklist   manual steps for the human, or "none"
```

## Findings format reviewers post
```
severity: Warning
location: orch/list.py:18
issue: Record files are read without handling invalid JSON; one bad file crashes the command.
recommendation: Catch json.JSONDecodeError, print the file name, continue with the rest.
```
Severity is one of `Critical`, `Warning`, `Suggestion`.

## Human controls
| You do | Runner does |
|---|---|
| Add `agent-fix` and leave a comment on the PR | Another build round addressing the comment |
| Close the PR | Stops; the work is abandoned |
| Merge the PR | Nothing more; merging is approval |

## Runners
Any harness that follows this file. Current runner: Hermes-Box, acting as `kallen-hermesbot`.

## Hard rules
- No API keys in the repo, in issues, or in PR bodies.
- Push only to `agent/*` branches. No deploys of any kind.
- The issue is the whole spec; if something is missing, label `agent-blocked` and ask.
- No silent failures: every error is surfaced with its real output.
- No new dependencies without a human approving them in the issue or PR.
