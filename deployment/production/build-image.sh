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

echo "Creating clean build context from ${source_revision}"
git -C "${repo_root}" archive "${source_revision}" | tar -x -C "${build_context}"

docker buildx build \
  --platform linux/amd64 \
  --load \
  --build-arg "SOURCE_REVISION=${source_revision}" \
  --build-arg "APP_VERSION=${app_version}" \
  --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --tag "${image}" \
  --file "${build_context}/deployment/production/Dockerfile" \
  "${build_context}"

image_id="$(docker image inspect --format '{{.Id}}' "${image}")"
image_size="$(docker image inspect --format '{{.Size}}' "${image}")"

printf 'IMAGE=%s\nIMAGE_ID=%s\nSOURCE_REVISION=%s\nSIZE_BYTES=%s\n' \
  "${image}" "${image_id}" "${source_revision}" "${image_size}"
