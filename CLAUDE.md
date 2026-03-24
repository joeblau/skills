# blau- Skills

Personal Claude Code skills under the `blau-` namespace.

## Conventions

- Each skill lives in a `blau-{name}/` directory with a `SKILL.md` file
- Skill names use kebab-case: `blau-cpr`, `blau-deploy`, etc.
- SKILL.md must have YAML frontmatter with `name:` and `description:`
- No build system — skills are plain markdown
- Install: `make install` | Uninstall: `make uninstall`

## Creating a New Skill

```bash
mkdir blau-myskill
```

Create `blau-myskill/SKILL.md`:

```yaml
---
name: blau-myskill
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /blau-myskill.
  What the skill does.
---

Instructions for Claude Code when this skill is invoked.
```

Then run `make install` to symlink into `~/.claude/skills/`.
