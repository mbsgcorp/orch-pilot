# AGENTS.md

For AI agents (Claude Code, Codex, any runner) working inside mbsgcorp/orch-pilot.

## Read first
Read `HARNESS.md` before doing anything. It defines the issue -> branch -> PR contract,
the labels, and the hard rules. This file covers how to behave inside a task.

## Branch discipline
- One task, one branch: `agent/<issue-number>`. Create it from the latest `main`.
- Never commit to, push to, or rebase `main`.
- Stage explicit paths only: `git add path/one path/two`. Never `git add -A` or `git add .`.

## Commits
- Message format: `agent(#<issue>): <summary>`, e.g. `agent(#42): add orch list command`.
- Small, reviewable commits. No unrelated reformatting.

## Evidence package
Every PR carries evidence, not claims:
- Files changed, with a one-sentence description each.
- Every command from the issue's Test Plan and its real output, with pass/fail counts.
- Known limitations and edge cases not handled. Say so plainly.
- If no automated tests exist, say that instead of omitting the line.

## Do not modify in a task branch
These change only through `orch enroll`:
- `.github/ISSUE_TEMPLATE/agent-task.yml`
- `AGENTS.md`
- `HARNESS.md`

## When stuck
If the issue is ambiguous or a rule would have to be broken, stop, comment on the issue with
what is missing, and apply `agent-blocked`. Do not guess on anything irreversible.
Bot identity for this repo: `kallen-hermesbot`.
