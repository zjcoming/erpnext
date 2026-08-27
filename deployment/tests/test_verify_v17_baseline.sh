#!/usr/bin/env bash
set -euo pipefail

repo_under_test="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
verify_script="$repo_under_test/deployment/verify-v17-baseline.sh"

if [[ ! -x "$verify_script" ]]; then
	echo "missing executable verifier: $verify_script" >&2
	exit 1
fi

fixture_root="$(mktemp -d)"
trap 'rm -rf -- "$fixture_root"' EXIT

fixture_repo="$fixture_root/erpnext"
fixture_bench="$fixture_root/frappe-bench"
fixture_frappe="$fixture_bench/apps/frappe"
fake_bin="$fixture_root/bin"

mkdir -p \
	"$fixture_repo/erpnext" \
	"$fixture_bench/sites" \
	"$fixture_frappe/frappe" \
	"$fake_bin"

git -C "$fixture_repo" init -q
git -C "$fixture_repo" config user.name "Baseline Test"
git -C "$fixture_repo" config user.email "baseline-test@example.com"
printf '__version__ = "17.0.0-dev"\n' > "$fixture_repo/erpnext/__init__.py"
git -C "$fixture_repo" add erpnext/__init__.py
git -C "$fixture_repo" commit -qm "fixture erpnext"
erpnext_base_commit="$(git -C "$fixture_repo" rev-parse HEAD)"
printf 'development change\n' > "$fixture_repo/feature.txt"
git -C "$fixture_repo" add feature.txt
git -C "$fixture_repo" commit -qm "fixture development change"
erpnext_head="$(git -C "$fixture_repo" rev-parse HEAD)"

git -C "$fixture_frappe" init -q
git -C "$fixture_frappe" config user.name "Baseline Test"
git -C "$fixture_frappe" config user.email "baseline-test@example.com"
printf '__version__ = "17.0.0-dev"\n' > "$fixture_frappe/frappe/__init__.py"
git -C "$fixture_frappe" add frappe/__init__.py
git -C "$fixture_frappe" commit -qm "fixture frappe"
frappe_commit="$(git -C "$fixture_frappe" rev-parse HEAD)"

printf 'frappe\nerpnext\nprocess_simplification\n' > "$fixture_bench/sites/apps.txt"

cat > "$fake_bin/python3" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
	echo "Python 3.14.2"
	exit 0
fi
exit 2
EOF

cat > "$fake_bin/node" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
	echo "v24.16.0"
	exit 0
fi
exit 2
EOF
chmod +x "$fake_bin/python3" "$fake_bin/node"

baseline_file="$fixture_root/baseline.env"
write_baseline() {
	local expected_erpnext_base="$1"
	local expected_frappe="$2"
	local release_ref="${3:-}"
	cat > "$baseline_file" <<EOF
BENCH_IMAGE=docker.io/frappe/bench:v5.31.0
FRAPPE_BOOTSTRAP_BRANCH=develop
FRAPPE_COMMIT=$expected_frappe
ERPNEXT_BASE_COMMIT=$expected_erpnext_base
ERPNEXT_RELEASE_REF=$release_ref
FRAPPE_VERSION=17.0.0-dev
ERPNEXT_VERSION=17.0.0-dev
PYTHON_VERSION=3.14.2
NODE_VERSION=24.16.0
CUSTOM_APP=process_simplification
EOF
}

run_verify() {
	REPO_ROOT="$fixture_repo" \
		BENCH_ROOT="$fixture_bench" \
		BASELINE_FILE="$baseline_file" \
		PATH="$fake_bin:$PATH" \
		"$verify_script" "$@"
}

write_baseline "$erpnext_base_commit" "$frappe_commit"
run_verify development > "$fixture_root/development-ok.log"
grep -q "Baseline verification passed (development)" "$fixture_root/development-ok.log"
grep -q "ERPNext HEAD has advanced from the pinned base" "$fixture_root/development-ok.log"

if run_verify production > "$fixture_root/production-missing-ref.log" 2>&1; then
	echo "production verification accepted a missing release ref" >&2
	exit 1
fi
grep -q "ERPNEXT_RELEASE_REF is required in production mode" "$fixture_root/production-missing-ref.log"

git -C "$fixture_repo" tag fixture-release "$erpnext_head"
write_baseline "$erpnext_base_commit" "$frappe_commit" "fixture-release"
run_verify production > "$fixture_root/production-ok.log"
grep -q "Baseline verification passed (production)" "$fixture_root/production-ok.log"

write_baseline "0000000000000000000000000000000000000000" "$frappe_commit"
if run_verify development > "$fixture_root/erpnext-base-mismatch.log" 2>&1; then
	echo "development verification accepted an unknown ERPNext base" >&2
	exit 1
fi
grep -q "ERPNext base commit is unavailable" "$fixture_root/erpnext-base-mismatch.log"

write_baseline "$erpnext_base_commit" "0000000000000000000000000000000000000000"
if run_verify development > "$fixture_root/frappe-mismatch.log" 2>&1; then
	echo "development verification accepted a Frappe commit mismatch" >&2
	exit 1
fi
grep -q "Frappe HEAD does not match the pinned baseline" "$fixture_root/frappe-mismatch.log"

echo "verify-v17-baseline tests passed"
