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
# The commit the v1.3.1 tag resolves to (git rev-parse v1.3.1^{commit} —
# v1.3.1 is an annotated tag, so this is NOT the same hash `git ls-remote`
# shows, which is the tag object, not the commit), pinned independently of
# the tag name so a moved/replaced tag can't silently change what gets built.
ARG CGIT_COMMIT=044821677c774cd24f25f1818ea51d09cc64b006
# Must match the GIT_VER pinned in this cgit release's own Makefile
# (https://git.zx2c4.com/cgit/plain/Makefile?h=vX.Y.Z) — cgit links Git's
# object files directly into cgit.cgi, so a mismatch can fail to build.
ARG GIT_VERSION=2.54.0

# kernel.org publishes an official signed sha256sums.asc for Git releases —
# pinned here so a corrupted download or unexpected upstream content change
# fails the build loudly instead of silently compiling something unverified.
# Recompute with: curl -fsSL <url> | sha256sum
ARG GIT_SHA256=f689162364c10de79ef89aa8dbf48731eb057e34edbbd20aca510ce0154681a3

########################################
# 1. Fetch stage — runs once, natively
########################################
# Pinned to the build host's own platform ($BUILDPLATFORM, a predefined
# buildx ARG) rather than each requested target platform. Without this,
# a multi-platform build (linux/amd64 + linux/arm64) would run this stage
# once per target platform — which used to mean both legs hit the same
# network resource concurrently. Fetching once and sharing the result via
# COPY avoids that and halves the download traffic besides.
FROM --platform=$BUILDPLATFORM alpine:${ALPINE_VERSION} AS fetch
ARG CGIT_VERSION
ARG CGIT_COMMIT
ARG GIT_VERSION
ARG GIT_SHA256

RUN apk add --no-cache curl tar xz git

# --retry-all-errors casts a wider net than the default retryable status
# codes (catches odd transient responses, not just 5xx/408/429); -f makes
# curl fail loudly on HTTP errors instead of writing an error page that
# would otherwise surface as a confusing "not an xz file" error.
ARG CURL_FLAGS="-fsSL --retry 5 --retry-all-errors --retry-connrefused --retry-delay 3"

WORKDIR /src

# git.zx2c4.com/cgit's own snapshot-tarball CGI endpoint has turned out to
# be flaky in practice (observed sustained 502s from CI, unrelated to this
# Dockerfile). Its git smart-HTTP endpoint is a separate, more
# battle-tested code path and stayed healthy throughout, so source is
# fetched via `git clone` instead — which also verifies object integrity
# as an inherent part of the protocol, so the explicit HEAD check below is
# defense-in-depth against a moved tag, not a substitute for a checksum.
# (git itself has no built-in HTTP retry option, hence the shell loop.)
RUN ok=0; \
    for i in 1 2 3 4 5; do \
        git clone --depth 1 --branch "v${CGIT_VERSION}" https://git.zx2c4.com/cgit cgit && { ok=1; break; }; \
        echo "clone attempt $i failed, retrying..." >&2; \
        rm -rf cgit; \
        sleep 5; \
    done; \
    [ "$ok" = "1" ] || { echo "git clone failed after 5 attempts" >&2; exit 1; }; \
    cd cgit \
    && [ "$(git rev-parse HEAD)" = "${CGIT_COMMIT}" ] \
    && rm -rf .git

# cgit links Git's own object files directly into cgit.cgi (rather than
# shelling out to a separate git binary), so the matching Git source has
# to be fetched and built alongside it. This replicates cgit's own
# `make get-git` target manually so the download can be checksummed first.
# `git clone` (without --recurse-submodules) still creates an empty
# directory at cgit's registered "git" submodule path, even though it's
# not populated — so cgit/git already exists here. `mv src existing-dir`
# moves src *into* that directory instead of replacing it (silently
# producing cgit/git/git-2.54.0/... with no Makefile at cgit/git itself,
# which made `make` fail near-instantly against an empty dir). Has to be
# removed first, same as cgit's own `make get-git` target does.
RUN curl ${CURL_FLAGS} "https://www.kernel.org/pub/software/scm/git/git-${GIT_VERSION}.tar.xz" -o git.tar.xz \
    && echo "${GIT_SHA256}  git.tar.xz" | sha256sum -c - \
    && tar -xf git.tar.xz \
    && rm -rf cgit/git \
    && mv "git-${GIT_VERSION}" cgit/git \
    && rm git.tar.xz

########################################
# 2. Build stage — runs per target platform
########################################
FROM alpine:${ALPINE_VERSION} AS build

RUN apk add --no-cache \
        build-base \
        pkgconf \
        perl \
        python3 \
        zlib-dev \
        openssl-dev \
        lua5.4-dev

COPY --from=fetch /src/cgit /usr/src/cgit
WORKDIR /usr/src/cgit

# Alpine's lua5.4 pkg-config file isn't in cgit's Lua autodetection list
# (luajit/lua/lua5.2/lua5.1), so it has to be specified explicitly or the
# build silently links without Lua filter support.
#
# NO_REGEX / NO_GETTEXT: musl libc (unlike glibc) doesn't implement the
# REG_STARTEND regex extension or ship libintl.h — both are standard,
# well-known flags for building Git on Alpine; Git's own compile error
# for the former literally names the flag to use.
#
# One `make ... install` call, not `make ... && make install`: `install`
# already depends on `all`, and splitting it into two separate `make`
# invocations meant only the first carried these flags — the second
# started a fresh make process without them, so cgit's own CFLAGS
# change-tracking (CGIT-CFLAGS) detected the difference and silently
# force-rebuilt cgit.o without the musl workarounds, right before install.
RUN make \
        LUA_PKGCONFIG=lua5.4 \
        NO_REGEX=NeedsStartEnd \
        NO_GETTEXT=YesPlease \
        DESTDIR=/out \
        install

########################################
# 3. Runtime stage
########################################
FROM alpine:${ALPINE_VERSION} AS runtime
ARG CGIT_VERSION

LABEL org.opencontainers.image.title="cgit" \
      org.opencontainers.image.description="cgit ${CGIT_VERSION}, self-contained, served by lighttpd" \
      org.opencontainers.image.version="${CGIT_VERSION}" \
      org.opencontainers.image.licenses="GPL-2.0-only"

# python3/py3-pygments/py3-markdown: cgit's own bundled about-filter
# (html-converters/md2html, for README rendering) and source-filter
# (syntax-highlighting.py) are Python scripts that import the pygments/
# markdown libraries directly rather than shelling out to a CLI tool —
# both packages are needed at *runtime* (these filters run per-request,
# not at build time).
RUN apk add --no-cache \
        lighttpd \
        lighttpd-mod-deflate \
        su-exec \
        zlib \
        openssl \
        lua5.4-libs \
        tzdata \
        python3 \
        py3-pygments \
        py3-markdown \
    && mkdir -p /var/cache/cgit /repos

COPY --from=build /out/var/www/htdocs/cgit /var/www/htdocs/cgit
COPY --from=build /out/usr/local/lib/cgit/filters /usr/local/lib/cgit/filters

COPY config/cgitrc /etc/cgitrc
COPY config/lighttpd.conf /etc/lighttpd/lighttpd.conf
# Local style preferences (fonts) layered AFTER cgit's
# own unmodified stylesheet via a second css= line in cgitrc — bumping
# CGIT_VERSION no longer needs any stylesheet re-merge, everything custom
# lives in this one small file.
COPY config/custom.css /var/www/htdocs/cgit/custom.css
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080
VOLUME ["/repos", "/var/cache/cgit"]

# Checks /cgit.css (a static file lighttpd serves directly) rather than
# / — cgit itself returns HTTP 404 for / whenever scan-path finds zero
# repos, which is completely legitimate (e.g. right after first deploy,
# before anything's been dropped into /repos yet), and wget --spider
# treats any non-2xx as failure. A healthcheck shouldn't couple
# "container is up and serving" to "happens to have repos configured".
#
# wget is a busybox applet already present in Alpine, so this needs no
# extra package. --spider avoids downloading the body, just checks the
# response.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null --spider http://127.0.0.1:8080/cgit.css || exit 1

# No USER here on purpose: the entrypoint needs to start as root to create
# a user at the requested PUID/PGID and chown the cache volume to it,
# then drops privileges itself via su-exec before exec'ing lighttpd. See
# docker-entrypoint.sh.
ENTRYPOINT ["docker-entrypoint.sh"]
