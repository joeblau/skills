#!/usr/bin/env bash
# Manage this checkout's skills with Bash 3.2+ and standard Unix tools.
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }
usage() {
  printf '%s\n' 'Usage: skills.sh install|uninstall|list|check [--agent claude|codex|kimi|shared|all] [--directory PATH] [skill ...]'
}
main() {
  local root source action agent=claude directory='' name dir link target conflict=0 missing=0
  local -a names dirs
  names=(); dirs=()
  root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
  source="$root/b/skills"
  action="${1:---help}"
  case "$action" in
    --help|-h) usage; return ;;
    install|uninstall|list|check) shift ;;
    *) usage; fail "Unknown action: $action" ;;
  esac
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent|--directory)
        [[ $# -ge 2 && -n "$2" ]] || fail "Missing value for $1"
        if [[ "$1" == --agent ]]; then agent="$2"; else directory="$2"; fi
        shift 2 ;;
      --help|-h) usage; return ;;
      --) shift; while [[ $# -gt 0 ]]; do names+=("$1"); shift; done ;;
      -*) fail "Unknown option: $1" ;;
      *) names+=("$1"); shift ;;
    esac
  done
  case "$agent" in
    claude) dirs=("$HOME/.claude/skills") ;;
    codex|shared) dirs=("$HOME/.agents/skills") ;;
    kimi) dirs=("${KIMI_CODE_HOME:-$HOME/.kimi-code}/skills") ;;
    all) dirs=("$HOME/.claude/skills" "$HOME/.agents/skills") ;;
    *) fail "Unknown agent: $agent" ;;
  esac
  if [[ -n "$directory" ]]; then
    [[ "$agent" != all ]] || fail '--directory requires a single agent'
    # Match a literal tilde supplied as an argument.
    # shellcheck disable=SC2088
    case "$directory" in '~') directory="$HOME" ;; '~/'*) directory="$HOME/${directory:2}" ;; esac
    dirs=("$directory")
  fi
  if [[ ${#names[@]} -eq 0 ]]; then
    for target in "$source"/*/SKILL.md; do
      [[ -f "$target" ]] || continue
      name="${target%/SKILL.md}"; names+=("${name##*/}")
    done
  fi
  [[ ${#names[@]} -gt 0 ]] || fail "No skills found in $source"
  for name in "${names[@]}"; do
    case "$name" in ''|*[!a-z0-9-]*) fail "Invalid skill name: $name" ;; esac
    [[ -f "$source/$name/SKILL.md" ]] || fail "Unknown skill: $name"
  done

  # Preflight all destinations before writing any selected links.
  if [[ "$action" == install ]]; then
    for dir in "${dirs[@]}"; do
      for name in "${names[@]}"; do
        link="$dir/$name"; target="$source/$name"
        if [[ -e "$link" || -L "$link" ]]; then
          if [[ ! -L "$link" || ! "$link" -ef "$target" ]]; then
            printf 'Preserved existing installation: %s\n' "$link" >&2
            conflict=1
          fi
        fi
      done
    done
    [[ "$conflict" -eq 0 ]] || fail 'Resolve these conflicts before installing.'
  fi
  for dir in "${dirs[@]}"; do
    for name in "${names[@]}"; do
      link="$dir/$name"; target="$source/$name"
      case "$action" in
        install)
          mkdir -p -- "$dir"
          [[ -L "$link" ]] || ln -s -- "$target" "$link"
          printf 'Linked: %s -> %s\n' "$link" "$target" ;;
        uninstall)
          if [[ -L "$link" && "$link" -ef "$target" ]]; then
            rm -- "$link"
            printf 'Unlinked: %s\n' "$link"
          elif [[ -e "$link" || -L "$link" ]]; then
            printf 'Preserved unrelated path: %s\n' "$link"
          fi ;;
        list|check)
          if [[ -L "$link" && "$link" -ef "$target" && -f "$link/SKILL.md" ]]; then
            printf 'OK: %s\n' "$link"
          else
            printf 'MISSING/OTHER: %s\n' "$link"
            missing=1
          fi ;;
      esac
    done
  done
  [[ "$action" != check || "$missing" -eq 0 ]]
}
main "$@"
