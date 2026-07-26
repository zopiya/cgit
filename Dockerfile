# syntax=docker/dockerfile:1
#
# cgit — a fast web interface for git, written in C — built from source
# against the exact Git release it's tested with, and served by lighttpd.
# Fully self-contained at runtime: no network access, no external "git"
# binary, no dependency on any particular domain, proxy, or VPN — plain
# HTTP on :8080. See README for how a domain/TLS layer can be added
# optionally, on top, without this image changing.
#
# Build:
#   docker build -t cgit:1.3.1 .
#
# Run:
#   docker run -d -p 8080:8080 \
#     -e PUID=1000 -e PGID=1000 \
#     -v /path/to/bare/repos:/repos:ro \
#     -v cgit-cache:/var/cache/cgit \
#     cgit:1.3.1
#
# Repos placed under /repos are auto-discovered (cgitrc: scan-path=/repos).

ARG ALPINE_VERSION=3.22
ARG CGIT_VERSION=1.3.1
# Must match the GIT_VER pinned in this cgit release's own Makefile
# (https://git.zx2c4.com/cgit/plain/Makefile?h=vX.Y.Z) — cgit links Git's
# object files directly into cgit.cgi, so a mismatch can fail to build.
ARG GIT_VERSION=2.54.0

# Pinned so a corrupted download, MITM'd mirror, or unexpected upstream
# content change fails the build loudly instead of silently compiling
# something unverified. Recompute with:
#   curl -fsSL <url> | sha256sum
# whenever CGIT_VERSION/GIT_VERSION above are bumped.
ARG CGIT_SHA256=c40fd71e120783d5e57d822208f3e17333cde2cd4baf3e7c8c75630b68afe12a
ARG GIT_SHA256=f689162364c10de79ef89aa8dbf48731eb057e34edbbd20aca510ce0154681a3

########################################
# 1. Build stage
########################################
FROM alpine:${ALPINE_VERSION} AS build
ARG CGIT_VERSION
ARG GIT_VERSION
ARG CGIT_SHA256
ARG GIT_SHA256

RUN apk add --no-cache \
        build-base \
        curl \
        tar \
        xz \
        pkgconf \
        perl \
        python3 \
        zlib-dev \
        openssl-dev \
        lua5.4-dev

# --retry* guards against transient network blips during CI/rebuilds;
# -f makes curl fail loudly on HTTP errors instead of writing an error
# page that would otherwise surface as a confusing "not an xz file" error.
ARG CURL_FLAGS="-fsSL --retry 3 --retry-connrefused --retry-delay 2"

WORKDIR /usr/src

RUN curl ${CURL_FLAGS} "https://git.zx2c4.com/cgit/snapshot/cgit-${CGIT_VERSION}.tar.xz" -o cgit.tar.xz \
    && echo "${CGIT_SHA256}  cgit.tar.xz" | sha256sum -c - \
    && tar -xf cgit.tar.xz \
    && mv "cgit-${CGIT_VERSION}" cgit \
    && rm cgit.tar.xz

WORKDIR /usr/src/cgit

# cgit links Git's own object files directly into cgit.cgi (rather than
# shelling out to a separate git binary), so the matching Git source has
# to be fetched and built alongside it. This replicates cgit's own
# `make get-git` target manually so the download can be checksummed first.
RUN curl ${CURL_FLAGS} "https://www.kernel.org/pub/software/scm/git/git-${GIT_VERSION}.tar.xz" -o git.tar.xz \
    && echo "${GIT_SHA256}  git.tar.xz" | sha256sum -c - \
    && rm -rf git \
    && tar -xf git.tar.xz \
    && mv "git-${GIT_VERSION}" git \
    && rm git.tar.xz

# Alpine's lua5.4 pkg-config file isn't in cgit's Lua autodetection list
# (luajit/lua/lua5.2/lua5.1), so it has to be specified explicitly or the
# build silently links without Lua filter support.
RUN make LUA_PKGCONFIG=lua5.4 \
    && make DESTDIR=/out install

########################################
# 2. Runtime stage
########################################
FROM alpine:${ALPINE_VERSION} AS runtime
ARG CGIT_VERSION

LABEL org.opencontainers.image.title="cgit" \
      org.opencontainers.image.description="cgit ${CGIT_VERSION}, self-contained, served by lighttpd" \
      org.opencontainers.image.version="${CGIT_VERSION}" \
      org.opencontainers.image.licenses="GPL-2.0-only"

RUN apk add --no-cache \
        lighttpd \
        su-exec \
        zlib \
        openssl \
        lua5.4-libs \
        tzdata \
    && mkdir -p /var/cache/cgit /repos

COPY --from=build /out/var/www/htdocs/cgit /var/www/htdocs/cgit
COPY --from=build /out/usr/local/lib/cgit/filters /usr/local/lib/cgit/filters

COPY config/cgitrc /etc/cgitrc
COPY config/lighttpd.conf /etc/lighttpd/lighttpd.conf
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080
VOLUME ["/repos", "/var/cache/cgit"]

# wget is a busybox applet already present in Alpine, so this needs no
# extra package. --spider avoids downloading the body, just checks the
# response.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null --spider http://127.0.0.1:8080/ || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
