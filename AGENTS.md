- Before opening/reading/editing files, preflight file paths first with `rg --files` or `fd` and then open the resolved path.

## Tools

- You are likely running in an environment where these following tools are available. If they are not and you need to use them, install them to do your work efficiently and effectively.

- **fd** – Fast, user-friendly file finder. Try `fd src` or `fd -e ts foo` for scoped searches; faster than `find` and honors `.gitignore`.
- **ripgrep (`rg`)** – Recursive code searcher. Examples: `rg "TODO"` or `rg -n --glob '!dist' foo`. Much faster than `grep`/`ack`, and respects `.gitignore`.
- **ast-grep (`sg`)** – AST-aware code search and refactor tool. Use `sg -p 'if ($A) { $B }'` to match syntax-aware patterns across languages.
- **jq** – JSON processor for quick transforms like `jq '.items[].id' resp.json`; ideal for inspecting API responses.
- **fzf** – General-purpose fuzzy finder. Pipe lists (`history | fzf`, `rg foo | fzf`) to jump through filtered results.
- **bat** – `cat` with syntax highlighting, paging, and git integration; e.g., `bat file.ts` or `bat -p README.md`.
- **eza** – Modern `ls` replacement with better defaults. Use `eza -l --git` or `eza -T` for tree views.
- **zoxide** – Smarter `cd` that learns your paths. Jump with `z foo` or `zi my/project` instead of typing long paths.
- **httpie** – Human-friendly HTTP client; `http GET api/foo` or `http POST api bar=1` provide readable JSON output.
- **git-delta (`delta`)** – Enhanced `git diff`/pager with syntax coloring; enable via `git -c core.pager=delta diff` for side-by-side diffs.
