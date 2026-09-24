# ctrlrtn — small, non-root, state under /var/lib/ctrlrtn.
#
#   docker build -t ctrlrtn .
#   docker run -d --name ctrlrtn \
#     -p 127.0.0.1:4000:4000 \
#     -v ctrlrtn-data:/var/lib/ctrlrtn \
#     ctrlrtn
#
# SECURITY: always publish on 127.0.0.1 (or a private interface). Docker's
# port publishing bypasses ufw-style host firewalls — a bare `-p 4000:4000`
# exposes the router (and its unauthenticated /ctrlrtn/ control plane) to the
# world. See docs/deploy.md.

FROM python:3.12-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# A plain (non-editable) install into an isolated venv we can copy out.
# (pip, not uv: one registry fewer to depend on; the dep tree is tiny.)
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim-bookworm
# curl for the HEALTHCHECK only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --home-dir /var/lib/ctrlrtn --create-home router
COPY --from=build /venv /venv

ENV PATH="/venv/bin:$PATH" \
    CTRLRTN_DB=/var/lib/ctrlrtn/router.db \
    CTRLRTN_HOST=0.0.0.0
# 0.0.0.0 INSIDE the container only — reachability is decided by how the port
# is published (see the header note). All CTRLRTN_* env vars work here.

USER router
VOLUME /var/lib/ctrlrtn
EXPOSE 4000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD curl -sf http://127.0.0.1:4000/healthz || exit 1
CMD ["ctrlrtn", "serve"]
