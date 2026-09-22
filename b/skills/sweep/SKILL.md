---
name: sweep
description: Merge outstanding Git branches and clean up merged remote branches, completed worktrees, and orphaned local branches to finish on a clean, synchronized main. Use for a repository sweep, branch cleanup with integration, or b:sweep.
---

# Sweep

Bring the current repository to a clean `main`: integrate outstanding work, publish the validated result, delete integrated topic branches remotely and locally, and remove completed worktrees. Treat `b:sweep` as a request for this workflow when this skill is available; it is not a shell alias.

## Scope and authorization

An explicit request to run sweep authorizes ordinary merges, pushes, and cleanup of verified integrated topic branches and completed worktrees in the selected repository. Honor narrower requests such as preview-only or local-only. Creating or editing this skill does not authorize running it against a repository.

Read applicable repository instructions. Use `main` unless the user names another integration branch; if absent, identify the repository's default branch. Select the publishing remote from the target's upstream and repository configuration; resolve genuine ambiguity before remote mutations. Do not sweep unrelated repositories or delete branches in forks or other remotes merely because they are visible.

Preserve the integration branch, remote default branches, protected branches, and intentional long-lived branches. Never discard dirty files, silently stash user work, force-push history, bypass required reviews/checks, or force-remove worktrees. A missing upstream does not make work disposable.

## Inventory

1. Inspect repository status, ongoing merge/rebase operations, remotes, branch tips/upstreams, and `git worktree list --porcelain`. Record candidate tip SHAs to detect concurrent changes.
2. Fetch the relevant remote with pruning. Pruning stale tracking refs and deleting actual remote branches are separate operations. If fetching fails, continue read-only diagnosis but do not delete branches based on stale information.
3. Inspect every worktree's tracked, untracked, and ignored files before considering removal. Preserve dirty, locked, active, or uncertain worktrees. A clean worktree is not necessarily completed: its branch or detached HEAD must be integrated and it must not be in active use. Preserve the primary worktree and the worktree where this run began.
4. Inventory local topic branches, topic branches on the publishing remote, and relevant PRs where hosting tools are available. Include local-only branches. Identify stacked dependencies, divergent local/remote tips, drafts, failing checks, and review requirements. A merged PR describes its recorded head, not necessarily the branch's current tip.
5. Classify candidates as integrated, ready to integrate, or blocked/retained with a concrete reason. Show a concise action summary and proceed within existing authorization without a routine confirmation gate.

## Integrate outstanding work

Use the existing clean target worktree if available. If it contains user changes, preserve it and integrate in an isolated worktree on a temporary branch based on the current target. Do not check out a branch held by another worktree or reset a divergent target.

Bring the target up to date without overwriting local commits. Integrate dependencies before dependents, sequentially, rechecking tips as you go. Review outstanding changes; branch names and clean status alone do not establish readiness. Drafts, explicit work-in-progress markers, missing required reviews, and unresolved semantic decisions are blockers to report.

For existing PRs, follow repository merge policy and required checks. Otherwise merge directly when permitted, normally preserving ancestry with a merge commit. Reconcile divergent local and remote topic tips so neither side's unique work is lost. Resolve mechanical conflicts where intent is clear; pause the affected integration when resolution requires a product decision. Continue independent eligible work.

Run repository-required validation appropriate to the combined changes before publishing. If hosting checks run after publication, verify those too before cleanup. Fix integration-caused failures within scope and rerun affected checks. Do not repeatedly retry unchanged failures or mark blocked branches completed. If the remote target advances, fetch, integrate, and validate again; never force-push it.

## Prove integration before cleanup

Cleanup requires evidence that the candidate's **current tip** is represented in the published target. Prefer `git merge-base --is-ancestor <candidate-tip> <published-target-tip>`. Check local and remote tips independently.

For squash/rebase merges, use hosting merge records with the exact merged head SHA and a merge result present in the target, or inspect patch equivalence and the resulting target content. `git cherry` and patch IDs aid investigation but alone do not prove integration of merge commits or later branch changes. Retain candidates when evidence is inconclusive.

A gone upstream, closed unmerged PR, old timestamp, clean worktree, or `--merged` against the wrong target does not justify deletion. An orphan with unique commits remains outstanding work: integrate it if eligible, otherwise retain it with a reason.

## Cleanup and finish

1. After validation and publication, recheck the published target and candidate tips. Delete verified integrated topic branches on the selected remote. Guard against concurrent changes, for example `git push --force-with-lease=refs/heads/<branch>:<verified-tip> <remote> :refs/heads/<branch>`. This is conditional deletion, not permission to rewrite history. A rejected lease requires fetching and reassessment.
2. Remove completed, clean, inactive linked worktrees with ordinary `git worktree remove`. Preserve worktrees containing tracked changes, untracked files, or ignored files; never force removal. Prune genuinely stale worktree metadata after inspection; preserve locked entries and temporarily unavailable paths.
3. Delete integrated local topic branches only after they are no longer checked out in any worktree. Prefer `git branch -d` after separately proving integration into the target; its own check can use an upstream. Use `-D` only for verified squash/rebase integration where ancestry prevents ordinary deletion, after rechecking the exact tip and worktree occupancy.
4. Fetch/prune again and reconcile the final inventory. Remove temporary integration branches/worktrees created by this run once published and eligible under the same cleanup checks.
5. Finish the starting worktree on `main` (or the selected target), synchronized with its publishing upstream, when possible without overwriting work or displacing another worktree's branch. Otherwise preserve existing work and report the target worktree path and remaining obstacle.

Report merged branches/PRs, deleted remote/local branches, removed worktrees, validation results, and anything retained or blocked. Claim a clean main only after verifying a clean working tree, no operation in progress, and equal local and published target tips. Claim the sweep complete only when every scoped branch and cleanup candidate is accounted for; distinguish preserved long-lived branches from blocked unfinished work.
