#!/usr/bin/env bash
# scripts/validate-skills.sh — sanity-check all 3 voice skills

set -uo pipefail
SKILLS_ROOT="$HOME/.claude/skills"
SKILLS=("voice-agent-realtime" "voice-translate-live" "voice-transcribe-stream")
FAIL=0

for skill in "${SKILLS[@]}"; do
  d="$SKILLS_ROOT/$skill"
  echo "== $skill =="

  # 1. SKILL.md exists
  if [ ! -f "$d/SKILL.md" ]; then
    echo "  MISSING SKILL.md"
    FAIL=1
    continue
  fi

  # 2. Frontmatter has name + description
  if ! head -10 "$d/SKILL.md" | grep -q "^name: $skill$"; then
    echo "  bad/missing name"
    FAIL=1
  fi
  if ! head -20 "$d/SKILL.md" | grep -q "^description: "; then
    echo "  bad/missing description"
    FAIL=1
  fi

  # 3. All Python files parse
  for f in "$d/examples/"*.py "$d/scripts/"*.py; do
    [ -f "$f" ] || continue
    if ! python3 -m py_compile "$f" 2>/dev/null; then
      echo "  py compile FAIL: ${f#$d/}"
      python3 -m py_compile "$f" 2>&1 | sed 's/^/    /'
      FAIL=1
    fi
  done

  # 4. All TS/TSX files type-check (single-file mode, lenient flags)
  for f in "$d/examples/"*.ts "$d/examples/"*.tsx; do
    [ -f "$f" ] || continue
    if ! npx --yes -p typescript tsc --noEmit --jsx preserve --esModuleInterop \
        --target es2022 --module esnext --moduleResolution bundler --skipLibCheck \
        --allowImportingTsExtensions --allowJs "$f" 2>/dev/null; then
      echo "  tsc FAIL: ${f#$d/}"
      FAIL=1
    fi
  done

  # 5. Markdown links to references/ resolve to existing files
  while IFS= read -r ref; do
    if [ ! -f "$d/$ref" ]; then
      echo "  broken ref: $ref"
      FAIL=1
    fi
  done < <(grep -oE 'references/[a-z0-9_-]+\.md' "$d/SKILL.md" 2>/dev/null | sort -u)
done

if [ "$FAIL" -ne 0 ]; then
  echo
  echo "VALIDATION FAILED"
  exit 1
fi
echo
echo "ALL SKILLS VALID"
