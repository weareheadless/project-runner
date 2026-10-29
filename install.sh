#!/bin/sh
# Copy agent definitions and rules from this repo into the places opencode and
# the workers read them from. Copies, not symlinks: commit first, then install.
set -eu
R=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENTS="$HOME/.config/opencode/agents"
RULES=/tmp/opencode

check=0
[ "${1:-}" = "--check" ] && check=1

drift=0
cmp_one() {
  src=$1 dst=$2
  if [ ! -f "$dst" ]; then
    echo "missing  $dst"; drift=1
  elif ! cmp -s "$src" "$dst"; then
    echo "differs  $dst"; drift=1
  fi
}

mkdir -p "$AGENTS" "$RULES"
for f in "$R"/agents/*.md; do
  dst="$AGENTS/$(basename "$f")"
  if [ "$check" = 1 ]; then cmp_one "$f" "$dst"; else cp "$f" "$dst"; fi
done
for f in "$R"/rules/LUNA-RULES.md; do
  dst="$RULES/$(basename "$f")"
  if [ "$check" = 1 ]; then cmp_one "$f" "$dst"; else cp "$f" "$dst"; fi
done

if [ "$check" = 1 ]; then
  [ "$drift" = 0 ] && echo "in sync" || echo "run ./install.sh to apply"
else
  echo "installed agents -> $AGENTS"
  echo "installed rules  -> $RULES/LUNA-RULES.md"
fi
