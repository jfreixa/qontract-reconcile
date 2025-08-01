# TODO: decommission once we remove ci.ext job completely
###############################################################################
# STAGE 1 - build-image
###############################################################################
FROM quay.io/redhat-services-prod/app-sre-tenant/container-images-master/qontract-reconcile-builder-master:1.0.0-1@sha256:bbfe9e1f33dda05c13960181be29484cc2db4c6e8aeeb731a50a1ac7ff11792f AS build-image
COPY --from=ghcr.io/astral-sh/uv:0.7.21@sha256:a64333b61f96312df88eafce95121b017cbff72033ab2dbc6398edb4f24a75dd /uv /bin/uv

WORKDIR /work

COPY pyproject.toml uv.lock README.md ./
COPY helm helm
COPY tools tools
COPY reconcile reconcile

ENV \
    # compile bytecode for faster startup
    UV_COMPILE_BYTECODE="true" \
    # disable uv cache. it doesn't make sense in a container
    UV_NO_CACHE=true

# Install dependencies and qontract-reconcile
RUN UV_DYNAMIC_VERSIONING_BYPASS="0.0.0" uv sync --frozen --no-group dev --python /usr/bin/python3


###############################################################################
# STAGE 2 - dev-image
###############################################################################
FROM quay.io/redhat-services-prod/app-sre-tenant/container-images-master/qontract-reconcile-base-master:1.0.0-1@sha256:6344b1751aec6cab930d1b60553930a3e139f2121620ab1a46673abec2b8ffbf AS dev-image
COPY --from=ghcr.io/astral-sh/uv:0.7.21@sha256:a64333b61f96312df88eafce95121b017cbff72033ab2dbc6398edb4f24a75dd /uv /bin/uv

ARG CONTAINER_UID=1000
RUN useradd --uid ${CONTAINER_UID} reconcile && \
    chown -R reconcile /.terraform.d

# Use a different workdir so venv can't be overwritten by volume mount
WORKDIR /opt/app-root/src

COPY --from=build-image --chown=reconcile:root /work/ ./
RUN UV_DYNAMIC_VERSIONING_BYPASS="0.0.0" uv sync --frozen --no-cache --group debugger --no-group dev

WORKDIR /work
USER reconcile
VOLUME ["/work", "/config"]
# Set the PATH to include the virtualenv
ENV PATH="/opt/app-root/src/.venv/bin:${PATH}"
ENTRYPOINT ["/work/dev/run.sh"]


###############################################################################
# STAGE 3 - prod-image
###############################################################################
FROM quay.io/redhat-services-prod/app-sre-tenant/container-images-master/qontract-reconcile-base-master:1.0.0-1@sha256:6344b1751aec6cab930d1b60553930a3e139f2121620ab1a46673abec2b8ffbf AS prod-image

ARG quay_expiration=never
LABEL quay.expires-after=${quay_expiration}

# Tini
ENV TINI_VERSION=v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN chmod +x /tini

# Keep the image up to date
RUN microdnf upgrade -y && microdnf clean all


WORKDIR /work
# the integration-manager needs the helm charts
RUN ln -s /work/helm /helm
COPY --from=build-image /work ./

# Set the PATH to include the virtualenv
ENV PATH="/work/.venv/bin:${PATH}"

ENTRYPOINT ["/tini", "--"]
CMD ["run-integration"]


###############################################################################
# STAGE 4 - fips-prod-image
###############################################################################
FROM prod-image AS fips-prod-image
ENV OC_VERSION=4.16.2

# oc versions sometimes have issues with FIPS enabled systems requiring us to use specific
# versions in these environments so in this case we extract an older version of oc and kubectl
COPY --chown=0:0 --from=quay.io/redhat-services-prod/app-sre-tenant/qontract-reconcile-oc-master:1.0.1@f8af926d49da49eec9083f9268d04a7cf34e8916 \
    /work/${OC_VERSION}/ /usr/local/bin/


###############################################################################
# STAGE 5 - unittest image
###############################################################################
FROM prod-image AS test-image
COPY --from=ghcr.io/astral-sh/uv:0.7.21@sha256:a64333b61f96312df88eafce95121b017cbff72033ab2dbc6398edb4f24a75dd /uv /bin/uv

RUN microdnf install -y make

# Tests need the .git directory to run
# podman does not support --mount=type=bind
# TODO: use --mount=source=.git,target=.git,type=bind in both RUN commands below
COPY .git .git

# Install test dependencies
RUN uv sync --frozen --no-cache --group dev

# Run tests
COPY Makefile .
RUN make all-tests

###############################################################################
# STAGE 6 - PyPI publish package
###############################################################################
FROM test-image AS pypi
ARG TWINE_USERNAME
ARG TWINE_PASSWORD

# TODO: use --mount=source=.git,target=.git,type=bind
# podman does not support --mount=type=bind
# qontract-reconcile version depends on git tags!
# Handled by konflux
#RUN make pypi
