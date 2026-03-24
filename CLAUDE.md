# b: Skills

Personal Claude Code skills under the `b:` namespace.

## Conventions

- Each skill lives in a `{name}/` directory with a `SKILL.md` file
- Directory names use kebab-case: `cpr`, `deploy`, etc.
- The `b:` prefix goes in the SKILL.md frontmatter `name:` field (e.g., `name: b:cpr`)
- Invoked as `/b:skillname` in Claude Code
- SKILL.md must have YAML frontmatter with `name:` and `description:`
- No build system — skills are plain markdown
- Install: `make install` | Uninstall: `make uninstall`

## Creating a New Skill

```bash
mkdir myskill
```

Create `myskill/SKILL.md`:

```yaml
---
name: b:myskill
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /b:myskill.
  What the skill does.
---

Instructions for Claude Code when this skill is invoked.
```

Then run `make install` to symlink into `~/.claude/skills/`.
