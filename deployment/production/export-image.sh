#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(git rev-parse --show-toplevel)"
image="${ERP_IMAGE:-hengsuan/erpnext:v16.33.0-hs.20260909.1}"
output_dir="${OUTPUT_DIR:-${repo_root}/outputs/releases}"
safe_name="$(printf '%s' "${image}" | tr '/:' '__')"

mkdir -p "${output_dir}"
docker image inspect "${image}" >/dev/null

if command -v zstd >/dev/null 2>&1; then
  archive="${output_dir}/${safe_name}.tar.zst"
  docker save "${image}" | zstd --threads=0 -10 --force -o "${archive}"
else
  archive="${output_dir}/${safe_name}.tar.gz"
  docker save "${image}" | gzip -6 > "${archive}"
fi

sha256sum "${archive}" > "${archive}.sha256"
du -h "${archive}" "${archive}.sha256"
