# syntax=docker/dockerfile:1.7

# These are linux/amd64 manifest digests, matching the Tencent Cloud host.
ARG BUILD_IMAGE=docker.io/frappe/build@sha256:9f2fb30900912dabb3c6e7c838f458e73f7009b6f90bd6bb73d5b917d3995359
ARG RUNTIME_IMAGE=docker.io/frappe/base@sha256:c7b5fd62f466f233781aeee0d606e877795562f192da5d72e98809ccf67db2bf

FROM ${BUILD_IMAGE} AS builder

ARG FRAPPE_REF=v16.32.0
ARG FRAPPE_COMMIT=5cba016e86b54b57f34a3864282b92300ef20fb0
ARG ERPNEXT_COMMIT=b24c9eba551905e256e336ff170a91a92d197a2f

USER frappe

COPY --chown=frappe:frappe deployment/production/apps.json /tmp/apps.json

RUN --mount=type=cache,target=/home/frappe/.cache/pip,uid=1000,gid=1000 \
    --mount=type=cache,target=/home/frappe/.cache/uv,uid=1000,gid=1000 \
    --mount=type=cache,target=/home/frappe/.cache/yarn,uid=1000,gid=1000 \
    bench init \
      --apps_path=/tmp/apps.json \
      --frappe-branch="${FRAPPE_REF}" \
      --frappe-path=https://github.com/frappe/frappe \
      --no-procfile \
      --no-backups \
      --skip-redis-config-generation \
      --verbose \
      /home/frappe/frappe-bench && \
    test "$(git -C /home/frappe/frappe-bench/apps/frappe rev-parse HEAD)" = "${FRAPPE_COMMIT}" && \
    test "$(git -C /home/frappe/frappe-bench/apps/erpnext rev-parse HEAD)" = "${ERPNEXT_COMMIT}"

WORKDIR /home/frappe/frappe-bench

# Copy only the custom app's install metadata and package. The build context is
# produced with git archive, so local sites, credentials and dirty files cannot
# enter the image.
COPY --chown=frappe:frappe custom_apps/process_simplification/pyproject.toml \
  custom_apps/process_simplification/README.md \
  apps/process_simplification/
COPY --chown=frappe:frappe custom_apps/process_simplification/process_simplification \
  apps/process_simplification/process_simplification

RUN --mount=type=cache,target=/home/frappe/.cache/pip,uid=1000,gid=1000 \
    ./env/bin/pip install --no-deps -e apps/process_simplification && \
    { grep -qxF process_simplification sites/apps.txt || printf '\n%s\n' process_simplification >> sites/apps.txt; } && \
    bench build --production --app process_simplification --force && \
    bench compile-po-to-mo --app process_simplification --locale zh --force

# Banking's JS workspace and source maps are build inputs only. Frappe's own
# node_modules stays because the websocket process uses it at runtime.
RUN rm -rf \
      apps/erpnext/banking/node_modules \
      apps/erpnext/node_modules \
      /home/frappe/.cache \
      /tmp/apps.json && \
    find sites/assets -type f -name '*.map' -delete && \
    find apps -mindepth 1 -path '*/.git' -prune -exec rm -rf '{}' + && \
    find apps -type d -name '__pycache__' -prune -exec rm -rf '{}' + && \
    find apps -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

FROM ${RUNTIME_IMAGE} AS runtime

ARG SOURCE_REVISION=unknown
ARG FRAPPE_COMMIT=5cba016e86b54b57f34a3864282b92300ef20fb0
ARG ERPNEXT_COMMIT=b24c9eba551905e256e336ff170a91a92d197a2f
ARG APP_VERSION=16.33.0-hs.20260909.1

LABEL org.opencontainers.image.title="恒算 ERP" \
      org.opencontainers.image.description="ERPNext v16 production image with process_simplification" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.vendor="重庆恒算科技有限公司" \
      org.opencontainers.image.source="https://github.com/zjcoming/erpnext"

USER frappe
COPY --from=builder --chown=frappe:frappe /home/frappe/frappe-bench /home/frappe/frappe-bench

WORKDIR /home/frappe/frappe-bench

RUN cp -a sites/assets ./assets && \
    rm -rf sites/assets && \
    printf '%s\n' \
      "APP_VERSION=${APP_VERSION}" \
      "SOURCE_REVISION=${SOURCE_REVISION}" \
      "FRAPPE_COMMIT=${FRAPPE_COMMIT}" \
      "ERPNEXT_COMMIT=${ERPNEXT_COMMIT}" \
      > /home/frappe/frappe-bench/release.env

VOLUME ["/home/frappe/frappe-bench/sites", "/home/frappe/frappe-bench/logs"]

USER root
COPY deployment/production/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY deployment/production/start.sh /usr/local/bin/start.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/start.sh

USER frappe
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/local/bin/start.sh"]
