# syntax=docker/dockerfile:1
# Residual Agents images. Build from the repository root:
#   docker build -f infra/container/Dockerfile --target runner -t residual-agents:1.0.0 .
#   docker build -f infra/container/Dockerfile --target judge  -t residual-agents-judge:1.0.0 .
# runner: Python 3.11 with the scientific stack, Node 24, dsh 0.1.5-rc.1 and this repository at /opt/ra.
# judge:  runner plus Claude Code (for infra/judge/judge.sh).
# No image is published, because it would redistribute dsh; build it yourself. dsh is installed from npm at build
# time and is not part of this repository.
# Base images: the paper's runs reported Python 3.11.15 built with GCC 14.2.0 on glibc 2.41 (Debian 13), which is
# the official python:3.11.15 image for Debian 13; see VERSIONS.md for every version and its evidence.

ARG PYTHON_IMAGE=python:3.11.15-trixie
# Node 24 for dsh (it needs >= 23.8 for built-in zstd); only the node binary and npm are copied from this image.
# The exact Node 24 release of the paper's runs was not recorded (see VERSIONS.md).
ARG NODE_IMAGE=node:24-bookworm-slim

FROM ${NODE_IMAGE} AS node

FROM ${PYTHON_IMAGE} AS runner
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    MPLBACKEND=Agg \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY infra/container/build_steps.sh /opt/ra/infra/container/build_steps.sh
RUN sh /opt/ra/infra/container/build_steps.sh system

COPY infra/container/requirements-paper.txt /opt/ra/infra/container/requirements-paper.txt
RUN sh /opt/ra/infra/container/build_steps.sh python

COPY infra/container/dsh/ /opt/ra/infra/container/dsh/
# dsh 0.1.5-rc.1 from npm; a package-lock.json placed in infra/container/dsh/ is used when present
RUN sh /opt/ra/infra/container/build_steps.sh dsh

COPY pyproject.toml README.md LICENSE /opt/ra/
COPY residual_agents /opt/ra/residual_agents
COPY infra /opt/ra/infra
RUN sh /opt/ra/infra/container/build_steps.sh ra

USER ra
WORKDIR /workspace
CMD ["/opt/ra/infra/run/run_in_container.sh", "--profile", "paper"]

FROM runner AS judge
USER root
ARG CLAUDE_CODE_VERSION=2.1.281
RUN CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION} sh /opt/ra/infra/container/build_steps.sh judge
USER ra
