# Start here (3 prompts)

**Setup:** unzip so these files sit in an empty folder (this is the future repo root), open that folder in Claude Code, and make sure `gh auth status` shows you logged in. Then paste the prompts below, one at a time.

## Prompt 1: create the repository and the tracking (no application code)

```
Read CLAUDE.md, then PLAN.md in full, then docs/SPEC.md Sections 1, 3 and 16. Do not write application code in this task.

1. Initialise git (default branch main) and commit the starter files with the message "chore: add spec, plan and working rules".
2. Check `gh auth status`. Create a PRIVATE GitHub repository named `receivables-agent` under my account and push main. If a repository with that name already exists, stop and ask me.
3. Create these labels: step, blocked, spec-conflict, slice-1, slice-2, slice-3, checkpoint-c1, slice-4, slice-5.
4. Create one milestone for each section of PLAN.md that contains steps (six milestones).
5. Create one issue per step in PLAN.md (64 in total). Title: "[S1.01] <step title>". Body: the step's Goal, Spec, Tests and Done-when lines. Labels: `step` plus the step's slice label. Set the milestone. Skip anything that already exists and report counts instead of recreating it.
6. Print a table of milestone, issue count, first and last issue number. Do not change any other repository setting. Then stop and wait for me.
```

Recommended once, in GitHub settings (you, not Claude): protect `main` so changes need a pull request and a passing CI check.

## Prompt 2: first step

```
/step S1.01
```
(If the `/step` skill is not loaded: "Implement step S1.01 of PLAN.md following the Tracking workflow in CLAUDE.md. Show me your plan first.")

## Prompt 3: every step after that

Merge the previous PR in GitHub, then:

```
/step
```
It picks the first unchecked step in `PLAN.md`, branches from an updated `main`, shows a plan, implements with tests, opens a PR, ticks the step, updates the log and the progress files, and stops.

## Where you see progress
- GitHub: milestones (one per slice), issues (one per step), one PR per step, tags at slice close-outs, CI status.
- Repo: the README progress block, `docs/PROGRESS.md` (percent per slice and the next step), `docs/PROGRESS_LOG.md` (tests, commit and PR per step), `docs/DECISIONS.md` (conflicts and deviations).
