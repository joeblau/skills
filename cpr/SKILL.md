---
name: b:cpr
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /b:cpr.
  Create and manage GitHub pull requests. Creates branch, commits,
  opens PR, watches CI, fixes failures, and merges.
---

Create a PR the proper way:

1. Create a branch using the conventional commit format
2. Commit changes to the branch
3. Create a pull request using `gh pr create`
4. Watch the build and make sure the build is successful
5. If the pull request fails, fix the build
6. Merge the pull request with `gh pr merge`, then rebase and delete the local branch
