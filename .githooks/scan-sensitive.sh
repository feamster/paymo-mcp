#!/usr/bin/env bash
# Scan text for data that must never land in this public repo:
# third-party email addresses, real invoice numbers, real dollar amounts,
# and (optionally) a private denylist of client/matter/person names.
#
# Usage: scan-sensitive.sh <file-with-text-to-scan> <label>
# The private denylist lives OUTSIDE the repo (default below; override with
# PAYMO_MCP_DENYLIST). Bypass in an emergency with `git commit --no-verify`.
set -u
input="$1"; label="$2"
denylist="${PAYMO_MCP_DENYLIST:-$HOME/Library/CloudStorage/Box-Box/mcp-auth/paymo/sensitive-terms.txt}"
fail=0

report() { echo "pre-commit: $label: $1" >&2; sed 's/^/    /' >&2; fail=1; }

# 1. Email addresses not on the allowlist.
allow='^(feamster@uchicago\.edu|feamster@gmail\.com|noreply@anthropic\.com|noreply@github\.com|notifications@paymoapp\.com|service@meliopayments\.com|x@y\.com|[^@]+@example\.(com|org))$'
hits=$(grep -oE '[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "$input" | grep -viE "$allow" | sort -u)
[ -n "$hits" ] && report "third-party email address" <<< "$hits"

# 2. Bare local-parts: a name followed by an at-sign with the domain stripped.
hits=$(grep -oE '(^|[^A-Za-z0-9._%+-])[A-Za-z][A-Za-z0-9._-]{1,}@($|[^A-Za-z0-9.-]|\.\.\.)' "$input" | grep -vE 'feamster@' | sort -u)
[ -n "$hits" ] && report "bare email local-part" <<< "$hits"

# 3. Real Paymo invoice numbers (placeholders like INV-YYYYMMDD-NNN are fine).
hits=$(grep -oE 'INV-20[0-9]{6}-[0-9]{3}' "$input" | sort -u)
[ -n "$hits" ] && report "real invoice number" <<< "$hits"

# 4. Dollar amounts with a thousands separator (use $X,XXX.XX in docs).
hits=$(grep -oE '\$[0-9]{1,3}(,[0-9]{3})+(\.[0-9]{2})?' "$input" | sort -u)
[ -n "$hits" ] && report "dollar amount" <<< "$hits"

# 5. Private denylist of client / matter / person names.
if [ -f "$denylist" ]; then
  while IFS= read -r term; do
    case "$term" in ''|'#'*) continue;; esac
    if grep -qiwF -- "$term" "$input"; then
      report "denylisted term '$term'" <<< "$(grep -niwF -- "$term" "$input" | head -3)"
    fi
  done < "$denylist"
else
  echo "pre-commit: warning: private denylist not found at $denylist (name check skipped)" >&2
fi

exit $fail
