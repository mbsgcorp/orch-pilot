# Contributing to orch-pilot

## Filing work
To request a change, open a new issue using the **Agent Task** form and fill in every required field. Submitting the form applies the `agent-ready` label, which tells a runner it may claim the issue right away. The issue body is the whole spec, so anything the runner needs to know must be written there.

## How the runner builds it
A runner claims the issue and builds it on its own branch named `agent/<issue-number>`. When the build is done, it opens a pull request titled `agent(#<issue>): <summary>` whose body carries the evidence, including the files changed and the real test output. See [HARNESS.md](HARNESS.md) for the full contract, including the labels and branch rules.

## Reviewing and merging
Two Codex reviews check each pull request and post their findings before a human looks at it. A human then decides, and merging the pull request is the approval. To ask for another round instead, add the `agent-fix` label and leave a comment on the pull request explaining what to change.
