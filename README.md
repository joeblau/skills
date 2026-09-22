# b: Skills

Six reusable skills for Claude Code, Codex, and Kimi Code. One source tree contains
shared instructions, scripts, and references; each agent supplies its own tools.

## One-command install (macOS, Linux, or WSL)

Run:

```bash
curl -fsSL https://raw.githubusercontent.com/joeblau/skills/main/install.sh | bash
```

This installs all six skills for Claude Code, Codex, and Kimi Code using a managed
checkout at `~/.local/share/b-skills/repo` (or `$XDG_DATA_HOME/b-skills/repo`).
Claude gets links in `~/.claude/skills`; Codex and Kimi share `~/.agents/skills`.
Git and Bash must already be available; no sudo or agent CLI is required.
This installs skills, not the agents or video-processing dependencies.

Re-run the same command to update. The installer only fast-forwards a clean `main`
checkout from the expected origin. It preserves local edits, local commits, and
existing skill installations from other sources, reporting conflicts for you to
resolve. Keep the managed checkout because the skill links point into it.

To select an agent or skills:

```bash
curl -fsSL https://raw.githubusercontent.com/joeblau/skills/main/install.sh | bash -s -- --agent codex cpr sweep
```

To inspect the script before running:

```bash
curl -fsSL https://raw.githubusercontent.com/joeblau/skills/main/install.sh -o /tmp/b-skills-install.sh
less /tmp/b-skills-install.sh
bash /tmp/b-skills-install.sh
```

`B_SKILLS_INSTALL_DIR` overrides the checkout location. `B_SKILLS_REPO_URL`
overrides the source repository (it must provide a `main` branch with this layout).
Set these on the `bash` process when piping, for example
`curl -fsSL <installer-url> | B_SKILLS_INSTALL_DIR=/path/to/repo bash`.
Uninstall links with
`bash ~/.local/share/b-skills/repo/scripts/skills.sh uninstall --agent all`
(adjust the path if overridden).

## Install for local agents

Clone the repository, then choose an installation:

```bash
git clone https://github.com/joeblau/skills.git
cd skills
make install AGENT=claude    # ~/.claude/skills (the default)
make install AGENT=shared    # ~/.agents/skills: discovered by Codex and Kimi
# Or install for a specific agent:
make install AGENT=codex     # ~/.agents/skills
make install AGENT=kimi     # ${KIMI_CODE_HOME:-~/.kimi-code}/skills
# Install for all three without duplicate Kimi entries:
make install-all            # Claude + shared directories
```

These commands create symlinks to this checkout, so keep it in place. Existing
files, directories, and links owned by another installation are preserved; an
install conflict stops before any selected links are created. Re-running an
installation is safe. Choose either shared or Kimi-specific installation to avoid
duplicate skill names. Restart the agent if new skills do not appear.

Select skills and manage the same destination with:

```bash
make skills
make install AGENT=shared SKILLS="cpr sweep"
make check AGENT=shared
make list AGENT=claude
make uninstall AGENT=shared SKILLS=sweep
make install AGENT=codex SKILLS_DIR=/custom/skills SKILLS=sweep
```

`SKILLS` defaults to all six skills. Unknown names fail before installation.
`AGENT=all` also works with `check`, `list`, and `uninstall`. Uninstall removes only
links pointing to this checkout. Bash and Make are needed for installation.

## Invoke

| Skill | Claude local | Claude plugin | Codex | Kimi Code |
|---|---|---|---|---|
| Pull request workflow | `/cpr` | `/b:cpr` | `$cpr` | `/skill:cpr` |
| Merge and clean Git work | `/sweep` | `/b:sweep` | `$sweep` | `/skill:sweep` |
| Render sludge video | `/sludge` | `/b:sludge` | `$sludge` | `/skill:sludge` |
| Select and render clips | `/sludgify` | `/b:sludgify` | `$sludgify` | `/skill:sludgify` |
| Draft X content | `/x-post` | `/b:x-post` | `$x-post` | `/skill:x-post` |
| Review app design | `/zandesign` | `/b:zandesign` | `$zandesign` | `/skill:zandesign` |

All skills except `sweep` require explicit invocation. Claude and Kimi use
`disable-model-invocation: true`; Codex uses
`policy.allow_implicit_invocation: false` in each skill's `agents/openai.yaml`.
`sweep` remains discoverable from matching requests.

## Requirements and capability fallbacks

- `cpr` and `sweep`: Git, shell access, and authenticated GitHub CLI (`gh`) for GitHub PR operations.
- `sludge` and `sludgify`: local Python/CLI video tools. Run
  `b/skills/sludge/scripts/preflight.sh` (add `--sludgify` for clip mining) to check
  dependencies. On macOS, the repository's `Brewfile` provides `brew bundle` setup.
  Install both skills for rendering; `sludgify` resolves its sibling renderer from
  the source tree, or accepts `--sludge /absolute/path/to/sludge.py`.
- `x-post`: X API access through `X_BEARER_TOKEN`, or the agent's web search tool.
  Uses subagents when available; otherwise runs research, strategy, creation, and
  review sequentially. Drafts live under
  `${XDG_DATA_HOME:-~/.local/share}/b-skills/x-post`, overridable with `XPOST_DATA`.
  It does not publish posts.
- `zandesign`: file access for static review; browser automation and screenshot
  tools for visual/flow checks. Uses the available browser tool, MCP server, or
  project Playwright setup. Reports unavailable checks as unverified. Native iOS
  checks require macOS/Xcode; `--static-only` works without a browser or simulator.

These are agent workflows: a model-only chat without shell, files, or required
external tools cannot execute all their steps. Tool availability and permissions
still depend on the host. The shared instructions do not require specific model IDs.

## Claude Code plugin installation

Inside Claude Code:

```
/plugin marketplace add joeblau/skills
/plugin install b@b-skills
```

This installs all six skills under `/b:<skill>`. Use either this plugin or the
Claude local symlinks to avoid duplicate entries. The Claude marketplace manifest
is not needed for Codex or Kimi's direct skill installations.

```
/plugin marketplace update b-skills
/plugin uninstall b@b-skills
/plugin marketplace remove b-skills
```

For plugin development, add the checkout as a local marketplace instead:

```
/plugin marketplace add /path/to/skills
/plugin install b@b-skills
```

## Development

Skills live in `b/skills/<name>/SKILL.md`. Keep `name` equal to the directory name.
Resolve bundled resources relative to that skill's resolved source directory;
keep writable output outside plugin/skill installations. Describe required
capabilities instead of assuming a particular agent's tool names.

```bash
make validate  # requires jq for JSON manifests
bash tests/install.sh
```

For a manual-only skill, add `disable-model-invocation: true` to its frontmatter
and `policy.allow_implicit_invocation: false` to `agents/openai.yaml`. Keep command
examples for the three supported hosts clear. Bump versions in both
`.claude-plugin/marketplace.json` and `b/.claude-plugin/plugin.json` for releases.
