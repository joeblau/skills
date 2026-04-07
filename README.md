# b: Skills

Personal [Claude Code](https://docs.anthropic.com/en/docs/claude-code) skills.

## Install

```bash
git clone <repo-url> && cd skills
brew bundle
make install
```

`brew bundle` installs CLI tools from the [Brewfile](Brewfile) (fd, ripgrep, ast-grep, jq, fzf, bat, eza, zoxide, httpie, git-delta). `make install` symlinks all skill directories into `~/.claude/skills/` so Claude Code discovers them automatically.

## Usage

Invoke any skill by typing `/b:skillname` in Claude Code.

## Available Skills

| Skill | Description |
|-------|-------------|
| `/b:cpr` | Create and manage GitHub pull requests |

## Commands

```bash
make install    # Symlink all skills into ~/.claude/skills/
make uninstall  # Remove all symlinks
make list       # Show installed skills
make check      # Verify symlinks point to valid targets
```

## Creating a New Skill

```bash
mkdir myskill
cat > myskill/SKILL.md << 'EOF'
---
name: b:myskill
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /b:myskill.
  What the skill does.
---

Instructions for Claude Code.
EOF

make install
```
