#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(git rev-parse --show-toplevel)"
compose_file="${repo_root}/deployment/production/compose.yaml"
image="${ERP_IMAGE:-hengsuan/erpnext:v16.33.0-hs.20260909.1}"
expected_revision="${EXPECTED_SOURCE_REVISION:-$(git -C "${repo_root}" rev-parse HEAD)}"
project_name="hengsuan-erp-acceptance"
acceptance_dir="$(mktemp -d -t hengsuan-erp-acceptance.XXXXXXXX)"
env_file="${acceptance_dir}/acceptance.env"
site_name="acceptance.localhost"
keep="${KEEP_ACCEPTANCE_STACK:-0}"

cleanup() {
  status=$?
  if [[ "${keep}" == "1" && ${status} -ne 0 ]]; then
    echo "Acceptance stack retained for inspection: ${project_name}" >&2
    echo "Environment file: ${env_file}" >&2
  else
    docker compose --project-name "${project_name}" --env-file "${env_file}" --file "${compose_file}" down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -rf "${acceptance_dir}"
  fi
  exit "${status}"
}
trap cleanup EXIT

mkdir -p "${acceptance_dir}/secrets"
openssl rand -base64 32 > "${acceptance_dir}/secrets/db_root_password.txt"
openssl rand -base64 24 > "${acceptance_dir}/secrets/admin_password.txt"
chmod 0600 "${acceptance_dir}/secrets/"*.txt

cat > "${env_file}" <<EOF
ERP_IMAGE=${image}
PULL_POLICY=never
SITE_NAME=${site_name}
FRONTEND_BIND=127.0.0.1:18080
DB_ROOT_PASSWORD_FILE=${acceptance_dir}/secrets/db_root_password.txt
ADMIN_PASSWORD_FILE=${acceptance_dir}/secrets/admin_password.txt
GUNICORN_WORKERS=3
GUNICORN_THREADS=4
EOF

docker image inspect "${image}" >/dev/null
docker compose --project-name "${project_name}" --env-file "${env_file}" --file "${compose_file}" config --quiet
docker compose --project-name "${project_name}" --env-file "${env_file}" --file "${compose_file}" up --detach --wait --wait-timeout 900

curl --fail --silent --show-error --header "Host: ${site_name}" http://127.0.0.1:18080/api/method/ping | grep -q 'pong'
curl --fail --silent --show-error --header "Host: ${site_name}" http://127.0.0.1:18080/assets/process_simplification/css/hengsuan_branding.css >/dev/null

docker compose --project-name "${project_name}" --env-file "${env_file}" --file "${compose_file}" exec -T \
  --env "SITE_NAME=${site_name}" \
  --env "EXPECTED_SOURCE_REVISION=${expected_revision}" \
  backend \
  bash -Eeuc '
    grep -qx "SOURCE_REVISION=$EXPECTED_SOURCE_REVISION" release.env
    grep -qx "FRAPPE_PATCH_MANIFEST_SHA256=$(sha256sum frappe-patches.json | cut -d " " -f 1)" release.env
    ./env/bin/python -c "import hashlib,json,pathlib; m=json.loads(pathlib.Path(\"frappe-patches.json\").read_text()); assert all(hashlib.sha256((pathlib.Path(\"apps/frappe\") / p).read_bytes()).hexdigest() == h[\"patched\"] for p,h in m[\"files\"].items()), \"Frappe patch checksum mismatch\""
    grep -qx "ERPNEXT_PATCH_MANIFEST_SHA256=$(sha256sum erpnext-patches.json | cut -d " " -f 1)" release.env
    ./env/bin/python -c "import hashlib,json,pathlib; m=json.loads(pathlib.Path(\"erpnext-patches.json\").read_text()); assert all(hashlib.sha256((pathlib.Path(\"apps/erpnext\") / p).read_bytes()).hexdigest() == h[\"patched\"] for p,h in m[\"files\"].items()), \"ERPNext patch checksum mismatch\""
    apps="$(bench --site "$SITE_NAME" list-apps --format text)"
    grep -qw frappe <<<"$apps"
    grep -qw erpnext <<<"$apps"
    grep -qw process_simplification <<<"$apps"
    jq -e ".allow_tests == false and .developer_mode == 0" "sites/$SITE_NAME/site_config.json" >/dev/null
  '

docker compose --project-name "${project_name}" --env-file "${env_file}" --file "${compose_file}" ps --all
docker compose --project-name "${project_name}" --env-file "${env_file}" --file "${compose_file}" exec -T \
  backend ./env/bin/python - "${site_name}" < "${repo_root}/deployment/production/check-site-navigation.py"

echo "Production image acceptance passed: ${image}"
