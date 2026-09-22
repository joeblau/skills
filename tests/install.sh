#!/usr/bin/env bash
# Isolated integration tests for the Bash installer and hosted bootstrap.
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/b-skills-test.XXXXXX")"
trap 'rm -rf -- "$test_root"' EXIT
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
reject() { if "$@" >"$test_root/rejected.log" 2>&1; then fail "Unexpected success: $*"; fi; }
mkdir -p "$test_root/bin"
# Prove no installer path calls Python, even on a machine where it exists.
for name in python python3; do
  cat > "$test_root/bin/$name" <<'BLOCK'
#!/bin/sh
printf 'Python was called\n' >&2
exit 99
BLOCK
  chmod +x "$test_root/bin/$name"
done
ln -s /bin/bash "$test_root/bin/bash"
isolated() {
  env HOME="$test_root/home" KIMI_CODE_HOME="$test_root/kimi" \
    PATH="$test_root/bin:$PATH" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    B_SKILLS_REPO_URL="$test_root/source" B_SKILLS_INSTALL_DIR="$test_root/managed checkout" "$@"
}
manage() { isolated /bin/bash "$repo_root/scripts/skills.sh" "$@"; }
bootstrap() { isolated /bin/bash -s -- "$@" < "$repo_root/install.sh"; }

manage install --agent all
manage install --agent all
manage check --agent all
[[ ! -e "$test_root/kimi" ]] || fail 'Duplicate Kimi installation'
manage uninstall --agent all
reject manage check --agent all
printf 'PASS: all-agent installation, repeat, checks, and uninstall\n'

mkdir -p "$test_root/home/.agents/skills/sweep"
printf keep > "$test_root/home/.agents/skills/sweep/keep"
reject manage install --agent all cpr sweep
[[ ! -e "$test_root/home/.claude/skills/cpr" ]] || fail 'Partial installation on conflict'
manage uninstall --agent shared sweep
[[ -f "$test_root/home/.agents/skills/sweep/keep" ]] || fail 'Deleted user work'
rm -r "$test_root/home/.agents/skills/sweep"
ln -s "$test_root/missing" "$test_root/home/.agents/skills/sweep"
reject manage install --agent shared sweep
manage uninstall --agent shared sweep
[[ -L "$test_root/home/.agents/skills/sweep" ]] || fail 'Deleted foreign link'
rm "$test_root/home/.agents/skills/sweep"
printf 'PASS: directories and broken foreign links preserved; conflict preflight\n'

manage install --agent kimi sweep
[[ -f "$test_root/kimi/skills/sweep/SKILL.md" && ! -e "$test_root/kimi/skills/cpr" ]] || fail 'Kimi subset'
reject manage install --agent unknown
reject manage install --agent codex unknown
reject manage install --agent all --directory "$test_root/custom"
reject manage install --agent codex ../sweep
reject manage install --agent
printf 'PASS: Kimi override, subset, and invalid arguments\n'

for action in install check uninstall; do
  isolated make -C "$repo_root" "$action" AGENT=codex "SKILLS_DIR=$test_root/custom skills" SKILLS=sweep
done
printf 'PASS: Make integration and paths containing spaces\n'

mkdir -p "$test_root/source/scripts" "$test_root/source/b/skills/sweep" "$test_root/source/b/skills/cpr"
cp "$repo_root/scripts/skills.sh" "$test_root/source/scripts/skills.sh"
cp "$repo_root/b/skills/sweep/SKILL.md" "$test_root/source/b/skills/sweep/SKILL.md"
cp "$repo_root/b/skills/cpr/SKILL.md" "$test_root/source/b/skills/cpr/SKILL.md"
isolated git -C "$test_root/source" init -b main
isolated git -C "$test_root/source" config user.name 'Installer Test'
isolated git -C "$test_root/source" config user.email installer@example.test
isolated git -C "$test_root/source" add .
isolated git -C "$test_root/source" commit -m initial
bootstrap --agent codex sweep
[[ -f "$test_root/home/.agents/skills/sweep/SKILL.md" && ! -e "$test_root/home/.agents/skills/cpr" ]] || fail 'Bootstrap subset'
bootstrap
bootstrap
printf '\nUpdated.\n' >> "$test_root/source/b/skills/sweep/SKILL.md"
isolated git -C "$test_root/source" commit -am update
bootstrap
[[ "$(tail -n 1 "$test_root/home/.agents/skills/sweep/SKILL.md")" == Updated. ]] || fail 'Update not visible through link'
printf 'PASS: piped bootstrap, subset, repeat, and fast-forward update without Python\n'

printf keep > "$test_root/managed checkout/local-work"
reject bootstrap
[[ -f "$test_root/managed checkout/local-work" ]] || fail 'Deleted checkout changes'
[[ ! -d "$test_root/managed checkout.install-lock" ]] || fail 'Leaked lock on failure'
rm "$test_root/managed checkout/local-work"
isolated git -C "$test_root/managed checkout" -c user.name=Test -c user.email=test@example.test commit --allow-empty -m local
before="$(git -C "$test_root/managed checkout" rev-parse HEAD)"
reject bootstrap
[[ "$(git -C "$test_root/managed checkout" rev-parse HEAD)" == "$before" ]] || fail 'Discarded local commit'
printf 'PASS: local changes and commits preserved; failure releases lock\n'

# A different destination exercises rejection without touching the managed checkout.
mkdir -p "$test_root/foreign"
printf keep > "$test_root/foreign/keep"
reject env HOME="$test_root/home" B_SKILLS_INSTALL_DIR="$test_root/foreign" /bin/bash "$repo_root/install.sh"
[[ -f "$test_root/foreign/keep" ]] || fail 'Deleted unrelated destination'
printf 'PASS: unrelated bootstrap destination preserved\nAll Bash installation tests passed.\n'
