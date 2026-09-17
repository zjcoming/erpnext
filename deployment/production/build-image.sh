#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(git rev-parse --show-toplevel)"
source_ref="${SOURCE_REF:-HEAD}"
source_revision="$(git -C "${repo_root}" rev-parse "${source_ref}^{commit}")"
image="${ERP_IMAGE:-hengsuan/erpnext:v16.33.0-hs.20260909.1}"
app_version="${APP_VERSION:-v16.33.0-hs.20260909.1}"
build_context="$(mktemp -d -t hengsuan-erp-build.XXXXXXXX)"

cleanup() {
  rm -rf "${build_context}"
}
trap cleanup EXIT

snapshot_sha256=committed
case "${SOURCE_MODE:-committed}" in
  committed)
    echo "Creating clean build context from ${source_revision}"
    git -C "${repo_root}" archive "${source_revision}" | tar -x -C "${build_context}"
    ;;
  working-tree)
    if [[ "${source_ref}" != "HEAD" ]]; then
      echo "Working-tree snapshots require SOURCE_REF=HEAD" >&2
      exit 1
    fi
    snapshot_sha256="$(python3 "${repo_root}/deployment/production/snapshot-build-context.py" "${repo_root}" "${build_context}")"
    source_revision="${source_revision}-worktree-${snapshot_sha256:0:12}"
    echo "Building explicit working-tree snapshot ${snapshot_sha256}"
    ;;
  *) echo "Unknown SOURCE_MODE" >&2; exit 1 ;;
esac
if [[ -n "${BUILD_EVIDENCE_DIR:-}" && -f "${build_context}/source-manifest.json" ]]; then
  mkdir -p "${BUILD_EVIDENCE_DIR}"
  cp "${build_context}/source-manifest.json" "${BUILD_EVIDENCE_DIR}/source-manifest.json"
fi

docker buildx build \
  --platform linux/amd64 \
  --load \
  --build-arg "SOURCE_REVISION=${source_revision}" \
  --build-arg "SOURCE_SNAPSHOT_SHA256=${snapshot_sha256}" \
  --build-arg "APP_VERSION=${app_version}" \
  --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --tag "${image}" \
  --file "${build_context}/deployment/production/Dockerfile" \
  "${build_context}"

image_id="$(docker image inspect --format '{{.Id}}' "${image}")"
image_size="$(docker image inspect --format '{{.Size}}' "${image}")"

printf 'IMAGE=%s\nIMAGE_ID=%s\nSOURCE_REVISION=%s\nSIZE_BYTES=%s\n' \
  "${image}" "${image_id}" "${source_revision}" "${image_size}"
