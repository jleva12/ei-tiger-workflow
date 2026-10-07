#!/bin/sh
set -eu

grammar_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
generator=${TREE_SITTER:-tree-sitter}
case "$("$generator" --version)" in
  'tree-sitter 0.25.0'*) ;;
  *) echo 'Grammar generation requires tree-sitter CLI 0.25.0' >&2; exit 1 ;;
esac

if [ "${1:-}" = --check ]; then
  generated_dir=$(mktemp -d)
  trap 'rm -rf "$generated_dir"' EXIT HUP INT TERM
  cp "$grammar_dir/grammar.js" "$grammar_dir/tree-sitter.json" "$generated_dir/"
  mkdir -p "$generated_dir/bindings/go"
  cp "$grammar_dir/bindings/go/binding.go" "$generated_dir/bindings/go/"
else
  generated_dir=$grammar_dir
fi

cd "$generated_dir"
"$generator" generate --abi 14
# cgo does not track included C files outside the binding package in its build
# cache. Update the Go source when generated parser.c changes to force a rebuild.
python3 - <<'PY'
from pathlib import Path
import hashlib
p = Path('bindings/go/binding.go')
lines = p.read_text().splitlines()
lines = [line for line in lines if not line.startswith('// Generated parser SHA-256:')]
lines.insert(1, '// Generated parser SHA-256: ' + hashlib.sha256(Path('src/parser.c').read_bytes()).hexdigest())
p.write_text('\n'.join(lines) + '\n')
PY

if [ "${1:-}" = --check ]; then
  diff -ru "$grammar_dir/src" "$generated_dir/src"
  diff -u "$grammar_dir/bindings/go/binding.go" "$generated_dir/bindings/go/binding.go"
fi
