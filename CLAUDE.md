# b: Skills

Personal Claude Code skills, packaged as the `b` plugin and served from this repo
as the `b-skills` marketplace.

## Layout

```
.claude-plugin/marketplace.json   # marketplace manifest (repo = marketplace)
b/                                # the "b" plugin
├── .claude-plugin/plugin.json    # plugin manifest, "name": "b"
└── skills/<name>/SKILL.md        # one directory per skill
```

## Conventions

- Each skill lives in `b/skills/{name}/` with a `SKILL.md` file
- Directory names use kebab-case: `cpr`, `x-post`, etc.
- SKILL.md frontmatter `name:` is the **bare** skill name (`name: cpr`), NOT `b:cpr` —
  the `b:` prefix comes from the plugin name, so plugin `b` + skill `cpr` gives `/b:cpr`.
  Putting `b:` in the frontmatter would produce `/b:b:cpr`.
- SKILL.md must have YAML frontmatter with `name:` and `description:`
- No build system — skills are plain markdown
- Skill content may reference `${CLAUDE_PLUGIN_ROOT}` (plugin install dir) and
  `${CLAUDE_PLUGIN_DATA}` (writable, survives updates). Never hardcode
  `~/.claude/skills/...`; it only exists on the symlink dev path.
- Anything a skill writes at runtime belongs under `${CLAUDE_PLUGIN_DATA}` — the plugin
  directory is replaced on update.

## Install

- Users: `/plugin marketplace add joeblau/skills` then `/plugin install b@b-skills`
- Local dev: `make install` symlinks `b/skills/*` into `~/.claude/skills/`
  (invoked as `/cpr`, without the `b:` prefix — personal skills have no namespace)
- Subset: `make install SKILLS=cpr` or `make install SKILLS="cpr x-post"`
  (same for `uninstall`/`check`); `make skills` lists the names
- `make validate` checks both manifests parse and every frontmatter name matches its
  directory — run it after adding or renaming a skill

## Creating a New Skill

```bash
mkdir b/skills/myskill
```

Create `b/skills/myskill/SKILL.md`:

```yaml
---
name: myskill
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /b:myskill.
  What the skill does.
---

Instructions for Claude Code when this skill is invoked.
```

Then `make validate && make install`. Bump `version` in both manifests so existing
plugin installs pick up the change.
