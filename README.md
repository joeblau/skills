# b: Skills

Personal [Claude Code](https://docs.anthropic.com/en/docs/claude-code) skills, packaged as a plugin.

## Install (inside Claude Code)

No clone, no `make`. In any Claude Code session:

```
/plugin marketplace add joeblau/skills
/plugin install b@b-skills
```

That installs the `b` plugin and all five skills. Invoke them as `/b:cpr`, `/b:sludge`, `/b:sludgify`, `/b:x-post`, `/b:zandesign`.

Some skills shell out to CLI tools — `ffmpeg`, `uv`, `whisper-cli`, `gh`. See the [Brewfile](Brewfile) for the full list; `brew bundle` installs them if you have the repo checked out.

### Updating

```
/plugin marketplace update b-skills
```

### Uninstalling

```
/plugin uninstall b@b-skills
/plugin marketplace remove b-skills
```

## Install (local development)

Working on the skills themselves? Symlink them into `~/.claude/skills/` so edits take effect immediately, with no reinstall step:

```bash
git clone https://github.com/joeblau/skills.git && cd skills
brew bundle
make install
```

Every target takes a `SKILLS` variable — pass one name or a quoted, space-separated list:

```bash
make skills                        # list the skill names this repo provides
make install SKILLS=cpr            # install a single skill
make install SKILLS="cpr x-post"   # install several
make uninstall SKILLS=sludge       # remove just that one
make check SKILLS=zandesign        # verify just that one
```

Unknown names fail before anything is linked. Omitting `SKILLS` acts on all skills.

Note the naming difference: symlinked skills are **personal** skills, so they invoke as `/cpr` and `/sludge` — the `b:` prefix comes from the plugin namespace and only applies to the plugin install. Pick one or the other; running both installs the same skill twice under two names.

To test the plugin path against your working copy instead, add the checkout as a local marketplace:

```
/plugin marketplace add /path/to/skills
/plugin install b@b-skills
```

## Available Skills

| Skill | Directory | Description |
|-------|-----------|-------------|
| `/b:cpr` | `b/skills/cpr/` | Create and manage GitHub pull requests |
| `/b:sludge` | `b/skills/sludge/` | Render sludge content — head-locked talking head over a filler clip, jump cut for pacing, with synced captions |
| `/b:sludgify` | `b/skills/sludgify/` | Mine a YouTube or X video for the clips that continue the house narrative, then render them with `/b:sludge` |
| `/b:x-post` | `b/skills/x-post/` | Research, generate, and review X content |
| `/b:zandesign` | `b/skills/zandesign/` | Deep design review of a React / React Native app, with `--fix` to apply the fixes |

## Layout

```
.claude-plugin/marketplace.json   # makes this repo a plugin marketplace
b/                                # the "b" plugin
├── .claude-plugin/plugin.json
└── skills/
    ├── cpr/SKILL.md
    ├── sludge/SKILL.md
    ├── sludgify/SKILL.md
    ├── x-post/SKILL.md
    └── zandesign/SKILL.md
```

The plugin is named `b` and each skill's frontmatter `name` is the bare skill name — that pairing is what produces `/b:<skill>`. `make validate` enforces it.

## Commands

```bash
make install    # Symlink skills into ~/.claude/skills/ (all, or SKILLS=...)
make uninstall  # Remove the symlinks (all, or SKILLS=...)
make list       # Show which skills are installed
make check      # Verify symlinks point to valid targets
make validate   # Verify the manifests parse and frontmatter names match dirs
make skills     # Print every skill name in this repo
```

## Creating a New Skill

```bash
mkdir b/skills/myskill
cat > b/skills/myskill/SKILL.md << 'SKILL'
---
name: myskill
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /b:myskill.
  What the skill does.
---

Instructions for Claude Code.
SKILL

make validate
make install SKILLS=myskill
```

Bump `version` in both `.claude-plugin/marketplace.json` and `b/.claude-plugin/plugin.json` so existing plugin installs pick up the change.
