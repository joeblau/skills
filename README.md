# blau- Skills

Personal [Claude Code](https://docs.anthropic.com/en/docs/claude-code) skills.

## Install

```bash
git clone <repo-url> && cd skills
make install
```

This symlinks all `blau-*` skills into `~/.claude/skills/` so Claude Code discovers them automatically.

## Usage

Invoke any skill by typing `/blau-skillname` in Claude Code.

## Available Skills

| Skill | Description |
|-------|-------------|
| `/blau-cpr` | Create and manage GitHub pull requests |

## Commands

```bash
make install    # Symlink all skills into ~/.claude/skills/
make uninstall  # Remove all blau-* symlinks
make list       # Show installed blau-* skills
make check      # Verify symlinks point to valid targets
```

## Creating a New Skill

```bash
mkdir blau-myskill
cat > blau-myskill/SKILL.md << 'EOF'
---
name: blau-myskill
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /blau-myskill.
  What the skill does.
---

Instructions for Claude Code.
EOF

make install
```
