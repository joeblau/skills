#!/usr/bin/env bash
# Hosted bootstrap for Claude Code, Codex, and Kimi Code skills.
set -euo pipefail

main() {
  if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'HELP'
Usage: bash install.sh [--agent all|claude|codex|kimi|shared] [skill ...]

Defaults to all skills for Claude Code, Codex, and Kimi Code.
Re-run the same command to update. Requires Git and Bash (macOS/Linux/WSL).

B_SKILLS_INSTALL_DIR  Managed checkout (default: $XDG_DATA_HOME/b-skills/repo,
                      or ~/.local/share/b-skills/repo)
B_SKILLS_REPO_URL     Source repository (default: https://github.com/joeblau/skills.git)

Examples:
  bash install.sh
  bash install.sh --agent codex cpr sweep
HELP
    return
  fi
  command -v git >/dev/null 2>&1 || { printf 'Required tool missing: git\n' >&2; return 1; }

  local repo_url install_dir parent lock
  repo_url="${B_SKILLS_REPO_URL:-https://github.com/joeblau/skills.git}"
  install_dir="${B_SKILLS_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/b-skills/repo}"
  # Match a literal tilde supplied in an environment override.
  # shellcheck disable=SC2088
  case "$install_dir" in "~") install_dir="$HOME" ;; "~/"*) install_dir="$HOME/${install_dir:2}" ;; esac
  parent="$(dirname "$install_dir")"
  mkdir -p "$parent"
  parent="$(cd -- "$parent" && pwd -P)"
  install_dir="$parent/$(basename "$install_dir")"
  lock="${install_dir}.install-lock"
  mkdir "$lock" 2>/dev/null || { printf 'Installation is already locked: %s\n' "$lock" >&2; return 1; }
  # The trap owns only the empty lock directory created above.
  trap 'rmdir "$lock"' EXIT

  if [[ -e "$install_dir" || -L "$install_dir" ]]; then
    if [[ -L "$install_dir" || ! -d "$install_dir/.git" ]]; then
      printf 'Preserving existing path; not a managed checkout: %s\n' "$install_dir" >&2
      exit 1
    fi
    if [[ "$(git -C "$install_dir" remote get-url origin)" != "$repo_url" ]]; then
      printf 'Preserving checkout with a different origin: %s\n' "$install_dir" >&2
      exit 1
    fi
    if [[ "$(git -C "$install_dir" branch --show-current)" != main || -n "$(git -C "$install_dir" status --porcelain --untracked-files=all)" ]]; then
      printf 'Preserving checkout with local changes or a non-main branch: %s\n' "$install_dir" >&2
      exit 1
    fi
    git -C "$install_dir" fetch origin main
    if ! git -C "$install_dir" merge-base --is-ancestor HEAD FETCH_HEAD; then
      printf 'Preserving checkout with local commits; cannot safely update.\n' >&2
      exit 1
    fi
    git -C "$install_dir" merge --ff-only FETCH_HEAD
  else
    git clone --branch main --single-branch -- "$repo_url" "$install_dir"
  fi

  if [[ ! -f "$install_dir/scripts/skills.sh" ]]; then
    printf 'Source does not contain the portable installer: %s\n' "$install_dir" >&2
    exit 1
  fi
  bash "$install_dir/scripts/skills.sh" install --agent all "$@"
  printf '\nSkills installed from %s\nRe-run this command to update. Restart agents if new skills do not appear.\n' "$install_dir"
  # Remove the lock while the local variable is still in scope.
  rmdir "$lock"
  trap - EXIT
}

# Wrapping execution in main ensures the whole script is read before it runs.
main "$@"
