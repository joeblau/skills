---
name: cpr
description: |
  MANUAL TRIGGER ONLY: use when the user explicitly invokes cpr through their agent’s skill command or asks to run this skill.
  Create and manage GitHub pull requests. Creates branch, commits,
  opens PR, watches CI, fixes failures, and merges.
disable-model-invocation: true
---

## Agent compatibility

Use this skill with Claude Code (`/b:cpr` as a plugin or `/cpr` locally), Codex (`$cpr`), or Kimi Code (`/skill:cpr`). Resolve bundled files relative to the directory containing this `SKILL.md`, following symlinks to the source directory. Use the host’s available file and shell tools; tool names are not requirements. Shell commands require a local execution environment and the listed dependencies.

Create a PR the proper way:

1. Create a branch using the conventional commit format
2. Commit changes to the branch
3. Create a pull request using `gh pr create`
4. Watch the build and make sure the build is successful
5. If the pull request fails, fix the build
6. Merge the pull request with `gh pr merge`, then rebase and delete the local branch
7. Clean up local and remote branches
