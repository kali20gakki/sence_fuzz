#!/usr/bin/env bash
# scene-fuzz runner self-test. No target-specific content.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SF="$ROOT/scripts/sf.py"
TMP="$(mktemp -d /tmp/scene-fuzz-selftest.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }

python3 "$SF" --version >/dev/null
mkdir -p "$TMP/target"
echo "hello" > "$TMP/target/README.md"
echo "int main(void){return 0;}" > "$TMP/target/main.c"

python3 "$SF" init --target "$TMP/target" --run-dir "$TMP/run" --run-id selftest >/dev/null
python3 "$SF" probe --run-dir "$TMP/run" >/dev/null
[ -f "$TMP/run/00-run-meta/tools.json" ] || fail "probe did not write tools.json"
[ -f "$TMP/run/00-run-meta/resources.json" ] || fail "probe did not write resources.json"

mkcase() {
  local id="$1" script="$2" resources="$3" exclusive="$4"
  mkdir -p "$TMP/run/04-cases/$id"
  cat > "$TMP/run/04-cases/$id/run.sh" <<EOF
#!/usr/bin/env bash
$script
EOF
  cat > "$TMP/run/04-cases/$id/case.json" <<EOF
{
  "id": "$id",
  "title": "self-test $id",
  "priority": "P0",
  "risk": "contract",
  "lineage": "new",
  "hypothesis": "runner behavior",
  "resources": $resources,
  "exclusive": $exclusive,
  "run": {"cmd": "bash run.sh", "cwd": ".", "timeout_sec": 10},
  "repeat": 1,
  "oracle": "exit code"
}
EOF
}

mkcase C-OK 'echo ok' '[]' false
mkcase C-FAIL 'echo bad >&2; exit 3' '[]' false
mkcase C-SLEEP1 'sleep 0.25; echo one' '["r1"]' false
mkcase C-SLEEP2 'sleep 0.25; echo two' '["r1"]' false

python3 "$SF" run --run-dir "$TMP/run" --case "$TMP/run/04-cases/C-OK/case.json" --repeat 2 >/dev/null
N=$(find "$TMP/run/05-evidence/C-OK" -name result.json | wc -l)
[ "$N" -eq 2 ] || fail "repeat did not produce 2 attempts (got $N)"

set +e
python3 "$SF" run --run-dir "$TMP/run" --case "$TMP/run/04-cases/C-FAIL/case.json" >/dev/null
RC=$?
set -e
[ "$RC" -ne 0 ] || fail "failing case returned 0"

cat > "$TMP/run/02-scenarios/case-list.json" <<'EOF'
{"cases": [
  {"id": "C-OK", "priority": "P0", "resources": []},
  {"id": "C-SLEEP1", "priority": "P0", "resources": ["r1"]},
  {"id": "C-SLEEP2", "priority": "P0", "resources": ["r1"]}
]}
EOF
python3 "$SF" run-suite --run-dir "$TMP/run" --case-list 02-scenarios/case-list.json --jobs 2 >/dev/null
python3 - "$TMP/run" <<'PY'
import json, sys
from pathlib import Path
run = Path(sys.argv[1])
def load(cid):
    return json.loads(next((run/"05-evidence"/cid).glob("attempt-*/result.json")).read_text())
a, b = load("C-SLEEP1"), load("C-SLEEP2")
overlap = a["started_epoch"] < b["finished_epoch"] and b["started_epoch"] < a["finished_epoch"]
assert not overlap, (a, b)
assert a["status"] == "pass" and b["status"] == "pass"
print("parallel resource lock ok")
PY

# check should fail before context/report
set +e
python3 "$SF" check --run-dir "$TMP/run" >/dev/null 2>&1
[ $? -ne 0 ] || fail "check passed without context/report"
set -e

for f in repo-map contracts usage-context usage-scenarios history-review risk-model tooling domain-kg; do
  echo "# $f" > "$TMP/run/01-context/$f.md"
  echo "selftest context content for $f." >> "$TMP/run/01-context/$f.md"
done
mkdir -p "$TMP/run/06-analysis/findings"
cat > "$TMP/run/REPORT.md" <<'EOF'
# Self-test report

## 1. Summary
selftest
## 2. Scope
selftest
## 3. Usage
selftest
## 4. System
selftest
## 5. Env
selftest
## 6. Matrix
selftest
## 7. Stability
selftest
## 8. Risk
selftest
## 9. Negative
selftest
## 10. Limits
selftest
## 11. Repro
selftest
## 12. Parallel
selftest
## 13. Next
selftest
EOF
cat > "$TMP/run/06-analysis/findings/F-001.md" <<'EOF'
---
id: F-001
severity: S3
status: not-a-bug
cases: [C-OK]
evidence:
  - 05-evidence/C-OK/attempt-1/result.json
---
# self-test
EOF

python3 "$SF" check --run-dir "$TMP/run" >/dev/null

# Evolution loop: init -> next -> record -> require-loop check.
EV="$ROOT/scripts/evolve.py"
python3 "$EV" init --run-dir "$TMP/run" >/dev/null
[ -f "$TMP/run/09-loop/state.json" ] || fail "evolve init missing state"
python3 "$EV" next --run-dir "$TMP/run" | grep -q '\[' || fail "evolve next empty"
python3 "$EV" record --run-dir "$TMP/run" --case C-OK --axis load --change "self-test" >/dev/null
python3 "$EV" record --run-dir "$TMP/run" --case C-FAIL --axis minimize --change "self-test fail" >/dev/null
python3 "$EV" next --run-dir "$TMP/run" | grep -q 'minimize-failure' || fail "evolve did not prioritize failure"
[ "$(wc -l < "$TMP/run/09-loop/iterations.jsonl")" -ge 3 ] || fail "iterations.jsonl too short"
python3 "$SF" check --run-dir "$TMP/run" --require-loop >/dev/null
echo "SELFTEST OK"
