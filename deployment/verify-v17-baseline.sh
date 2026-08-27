#!/usr/bin/env bash
set -euo pipefail

mode="${1:-development}"
if [[ "$mode" != "development" && "$mode" != "production" ]]; then
	echo "Usage: $0 [development|production]" >&2
	exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="${REPO_ROOT:-$(cd "$script_dir/.." && pwd -P)}"
bench_root="${BENCH_ROOT:-$repo_root/development/frappe-bench}"
baseline_file="${BASELINE_FILE:-$repo_root/deployment/v17-baseline.env}"
compose_file="${COMPOSE_FILE:-$repo_root/docker-compose.yml}"

if [[ ! -f "$baseline_file" ]]; then
	echo "Baseline file not found: $baseline_file" >&2
	exit 1
fi

baseline_keys=(
	BENCH_IMAGE
	FRAPPE_BOOTSTRAP_BRANCH
	FRAPPE_COMMIT
	ERPNEXT_BASE_COMMIT
	ERPNEXT_RELEASE_REF
	FRAPPE_VERSION
	ERPNEXT_VERSION
	PYTHON_VERSION
	NODE_VERSION
	CUSTOM_APP
)

# Explicit environment values override the version-controlled defaults.
declare -A environment_overrides=()
for key in "${baseline_keys[@]}"; do
	if [[ -v "$key" ]]; then
		environment_overrides["$key"]="${!key}"
	fi
done

set -a
# shellcheck disable=SC1090
source "$baseline_file"
set +a

for key in "${!environment_overrides[@]}"; do
	printf -v "$key" '%s' "${environment_overrides[$key]}"
	export "$key"
done

errors=0
fail() {
	echo "$1" >&2
	errors=1
}

for key in BENCH_IMAGE FRAPPE_BOOTSTRAP_BRANCH FRAPPE_COMMIT ERPNEXT_BASE_COMMIT FRAPPE_VERSION ERPNEXT_VERSION PYTHON_VERSION NODE_VERSION CUSTOM_APP; do
	if [[ -z "${!key:-}" ]]; then
		fail "$key is missing from the baseline"
	fi
done

frappe_root="$bench_root/apps/frappe"
if ! git -C "$repo_root" rev-parse --git-dir >/dev/null 2>&1; then
	fail "ERPNext repository is unavailable: $repo_root"
fi
if ! git -C "$frappe_root" rev-parse --git-dir >/dev/null 2>&1; then
	fail "Frappe repository is unavailable: $frappe_root"
fi

if [[ "$errors" -eq 0 ]]; then
	if ! git -C "$repo_root" cat-file -e "${ERPNEXT_BASE_COMMIT}^{commit}" 2>/dev/null; then
		fail "ERPNext base commit is unavailable: $ERPNEXT_BASE_COMMIT"
	elif ! git -C "$repo_root" merge-base --is-ancestor "$ERPNEXT_BASE_COMMIT" HEAD; then
		fail "ERPNext HEAD does not descend from the pinned base: $ERPNEXT_BASE_COMMIT"
	else
		erpnext_head="$(git -C "$repo_root" rev-parse HEAD)"
		if [[ "$mode" == "development" && "$erpnext_head" != "$ERPNEXT_BASE_COMMIT" ]]; then
			echo "ERPNext HEAD has advanced from the pinned base: $erpnext_head"
		fi
	fi

	frappe_head="$(git -C "$frappe_root" rev-parse HEAD)"
	if [[ "$frappe_head" != "$FRAPPE_COMMIT" ]]; then
		fail "Frappe HEAD does not match the pinned baseline: expected $FRAPPE_COMMIT, got $frappe_head"
	fi

	if [[ -n "$(git -C "$frappe_root" status --porcelain)" ]]; then
		fail "Frappe worktree is not clean"
	fi
fi

read_version() {
	local version_file="$1"
	sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$version_file" | head -n 1
}

erpnext_version_file="$repo_root/erpnext/__init__.py"
frappe_version_file="$frappe_root/frappe/__init__.py"
if [[ ! -f "$erpnext_version_file" ]]; then
	fail "ERPNext version file is unavailable: $erpnext_version_file"
else
	actual_erpnext_version="$(read_version "$erpnext_version_file")"
	if [[ "$actual_erpnext_version" != "${ERPNEXT_VERSION:-}" ]]; then
		fail "ERPNext version mismatch: expected ${ERPNEXT_VERSION:-<missing>}, got ${actual_erpnext_version:-<missing>}"
	fi
fi

if [[ ! -f "$frappe_version_file" ]]; then
	fail "Frappe version file is unavailable: $frappe_version_file"
else
	actual_frappe_version="$(read_version "$frappe_version_file")"
	if [[ "$actual_frappe_version" != "${FRAPPE_VERSION:-}" ]]; then
		fail "Frappe version mismatch: expected ${FRAPPE_VERSION:-<missing>}, got ${actual_frappe_version:-<missing>}"
	fi
fi

python_executable=python3
if [[ -x "$bench_root/env/bin/python" ]]; then
	python_executable="$bench_root/env/bin/python"
fi
if actual_python_version="$($python_executable --version 2>&1 | awk '{print $2}')"; then
	if [[ "$actual_python_version" != "${PYTHON_VERSION:-}" ]]; then
		fail "Python version mismatch: expected ${PYTHON_VERSION:-<missing>}, got ${actual_python_version:-<missing>}"
	fi
else
	fail "Python executable is unavailable: $python_executable"
fi

if actual_node_version="$(node --version 2>&1)"; then
	actual_node_version="${actual_node_version#v}"
	if [[ "$actual_node_version" != "${NODE_VERSION:-}" ]]; then
		fail "Node version mismatch: expected ${NODE_VERSION:-<missing>}, got ${actual_node_version:-<missing>}"
	fi
else
	fail "Node executable is unavailable"
fi

apps_file="$bench_root/sites/apps.txt"
if [[ ! -f "$apps_file" ]] || ! grep -qx "${CUSTOM_APP:-}" "$apps_file"; then
	fail "Custom app is missing from sites/apps.txt: ${CUSTOM_APP:-<missing>}"
fi

if [[ "$mode" == "production" ]]; then
	if [[ -z "${ERPNEXT_RELEASE_REF:-}" ]]; then
		fail "ERPNEXT_RELEASE_REF is required in production mode"
	elif ! release_commit="$(git -C "$repo_root" rev-parse "${ERPNEXT_RELEASE_REF}^{commit}" 2>/dev/null)"; then
		fail "ERPNext release ref is unavailable: $ERPNEXT_RELEASE_REF"
	elif [[ "$release_commit" != "$(git -C "$repo_root" rev-parse HEAD)" ]]; then
		fail "ERPNext HEAD does not match release ref $ERPNEXT_RELEASE_REF"
	fi

	if [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
		fail "ERPNext worktree must be clean in production mode"
	fi
elif [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
	echo "Warning: ERPNext worktree has development changes" >&2
fi

if [[ -f "$compose_file" ]]; then
	if ! docker compose version >/dev/null 2>&1; then
		fail "Docker Compose is unavailable"
	elif compose_config="$(docker compose --env-file "$baseline_file" -f "$compose_file" config 2>&1)"; then
		if ! grep -Fq "image: ${BENCH_IMAGE:-}" <<<"$compose_config"; then
			fail "Compose does not resolve to the pinned Bench image: ${BENCH_IMAGE:-<missing>}"
		fi
		if ! grep -Fq "FRAPPE_COMMIT: ${FRAPPE_COMMIT:-}" <<<"$compose_config"; then
			fail "Compose does not expose the pinned Frappe commit"
		fi
		if ! grep -Fq "ERPNEXT_BASE_COMMIT: ${ERPNEXT_BASE_COMMIT:-}" <<<"$compose_config"; then
			fail "Compose does not expose the pinned ERPNext base commit"
		fi
	else
		fail "Docker Compose configuration is invalid: $compose_config"
	fi
fi

if [[ "$errors" -ne 0 ]]; then
	exit 1
fi

echo "Baseline verification passed ($mode)"
