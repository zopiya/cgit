# cgit Docker deployment

Builds [cgit](https://git.zx2c4.com/cgit/) `v1.3.1` from source against the
exact Git release it's pinned to (`2.54.0`), served by lighttpd on port
`8080`. Nothing else — no reverse proxy, no TLS, no domain baked in.

Live at `ghcr.io/zopiya/cgit` — built and smoke-tested by CI on every push
to `main`.

## Design: self-contained, no external dependencies

This is meant to be a source of truth for GitOps repos (possibly including
the config that deploys other infrastructure), so it must never end up
gated behind something it might itself be used to bootstrap:

- `cgit.cgi` makes zero network calls at runtime. Fetching the cgit/Git
  source only happens once, at `docker build` time, and every download is
  integrity-checked (see `Dockerfile`) — the running container never
  phones home to anything.
- `docker-compose.yml` publishes plain `http://<nas-ip>:8080/` directly on
  the host. No VPN, no DNS record, no proxy has to exist or be up for this
  to work — it's reachable the moment the container starts, over LAN or
  over a Tailscale IP, since both are just routes to the same bound port.
- Domain names / HTTPS are intentionally out of scope here. If you want
  `https://cgit.example.com`, run a reverse proxy (Caddy or otherwise) as
  its own separate deployment, pointed at this NAS's IP on `:8080` — this
  repo doesn't need to know that layer exists, and stays working if it's
  ever redeployed, replaced, or temporarily down.

## Build & run

```sh
docker compose up -d --build
```

Drop bare repositories (`myproject.git`) under whatever host path you bind
to `/repos` in `docker-compose.yml` (edit that path first) —
`cgitrc`'s `scan-path=/repos` auto-discovers them, no manual repo list to
maintain. They show up without the `.git` suffix (`remove-suffix=1`), and
per-repo metadata (description, owner, section, visibility) lives in each
repo's own git config — see Configuration below.

- `PUID`/`PGID` — uid/gid lighttpd runs as inside the container, so it can
  read repos owned by your NAS user. The entrypoint reuses an existing
  account at that id if one already exists in the image, so uncommon ids
  (e.g. a NAS's own admin uid) won't collide/fail.
- `TZ` — optional, e.g. `TZ=Asia/Shanghai`, for correctly localized
  timestamps in logs and in cgit's own UI. Defaults to UTC.

Open `http://<nas-lan-ip>:8080/` or `http://<nas-tailscale-ip>:8080/` —
both work identically, it's the same bound port either way.

Before anything's dropped into `/repos`, that page returns **HTTP 404**
with a "No repositories found" body — that's cgit's own normal behavior
for an empty repo list, not a broken deployment. (Because of this, the
`HEALTHCHECK` and CI's smoke test both check `/cgit.css` — a static file
lighttpd serves directly — rather than `/`, so an empty repo list is
never mistaken for the container being down.)

A `HEALTHCHECK` is built into the image (`docker ps` / `docker inspect`
will show container health). `docker logs cgit` shows errors (lighttpd's
own startup/runtime errors go to its default stderr); per-request access
logs go to `/var/cache/cgit/access.log` inside the cache volume instead —
`accesslog.filename` has no "just use the inherited stdout fd" fallback
the way the error log does, and pointing it at `/dev/stdout` directly
doesn't work once lighttpd has dropped to a non-root PUID (see
`config/lighttpd.conf` for the full explanation). View with
`docker exec cgit tail -f /var/cache/cgit/access.log`.
There's no rotation — at home-NAS traffic it grows slowly, and since
lighttpd opens the log with `O_APPEND`, reclaiming space is just
`docker exec cgit sh -c ': > /var/cache/cgit/access.log'` on the live
container; no restart needed.

The compose file also does some cheap hardening: it drops every Linux
capability except the five the entrypoint's startup sequence needs
(creating the PUID/PGID account, chowning the cache volume — the
`su-exec` privilege drop afterwards is permanent), sets
`no-new-privileges`, and puts the CGI upload spool on a `tmpfs`.
`read_only: true` is deliberately not set: the runtime user creation
writes `/etc/passwd` inside the container.

## Configuration

- `config/cgitrc` — baked into the image at `/etc/cgitrc`. Override at
  runtime by bind-mounting your own file over `/etc/cgitrc`. Per-repo
  settings come from each repo's own git config (`enable-git-config=1`),
  not a central list — set them from anywhere you can run git against the
  bare repo (e.g. over SSH to the NAS), no container restart involved:
  ```sh
  git -C /volume1/git-repos/myproject.git config cgit.desc "My project"
  git -C /volume1/git-repos/myproject.git config gitweb.owner "zopiya"
  git -C /volume1/git-repos/myproject.git config gitweb.category "infra"  # index section
  git -C /volume1/git-repos/myproject.git config cgit.hide 1              # serve but hide from index
  ```
  (`gitweb.owner`/`category`/`description`/`homepage` map to cgit's
  `repo.owner`/`section`/`desc`/`homepage`; any `cgit.*` key maps to the
  matching `repo.*` setting — `cgit.defbranch`, `cgit.snapshots`,
  `cgit.max-stats`, ... see `cgitrc(5)`.)
- `config/lighttpd.conf` — CGI routing; shouldn't need changes for normal
  use. `clone-url` in `cgitrc` derives from the request's `Host` header
  rather than a hardcoded name, so `git clone` URLs shown in the UI are
  correct whether you're hitting the NAS by LAN IP, Tailscale IP, or (if
  you later add one) a reverse-proxied domain. Text responses are
  gzip-compressed by lighttpd (`mod_deflate`) — felt mostly off-LAN,
  over Tailscale.
- `config/custom.css` — local style preferences (modern system font
  stacks instead of bare `sans-serif`/`monospace`, 14px instead of
  10pt), layered *after* cgit's own default stylesheet, which is served
  unmodified straight from the build (`css=` may be repeated; each
  entry becomes its own `<link>`). Bumping `CGIT_VERSION` therefore
  needs no stylesheet re-merge — edit this one small file for any
  look-and-feel change.

Enabled beyond the defaults: README rendering on each repo's "about" tab
(`readme`/`about-filter`, Markdown → HTML via `py3-markdown`), syntax
highlighting in the tree/blob view (`source-filter`, via `py3-pygments`),
a per-repo commit-activity chart (`max-stats`), an owner column
(`enable-index-owner`), branches sorted by recent activity
(`branch-sort=age`), a 512 KB cap on rendered blob size so a stray
`package-lock.json` can't hang a browser tab (`max-blob-size`), several
common README filename variants (`readme=:README.md`, `:readme.md`,
`:README.markdown`, ..., first match wins), per-repo settings from each
repo's own git config (`enable-git-config`, see above), `.git` suffixes
stripped from repo URLs and names (`remove-suffix`), and
`section-from-path` (repos get grouped by their first path component
under `scan-path` for free once/if they're ever organized into category
subdirectories — flat `/repos/*.git` is unaffected; a per-repo
`gitweb.category` overrides it). `module-link` cross-links submodule entries to their own
repo page here, but only resolves correctly if a submodule's checkout
directory name matches its corresponding repo's name on this instance —
see the comment above it in `config/cgitrc` for why.

Cloning from this instance works out of the box: cgit serves git's dumb
HTTP protocol itself — `info/refs` and the pack list are generated
per-request from the live repo, so there's no `git update-server-info`
hook to install and nothing to re-run after pushing. Two limitations of
dumb HTTP worth knowing: no shallow or partial clones (`--depth`,
`--filter`), and transfers are whole-pack with no negotiation —
irrelevant at personal-repo sizes, worth knowing before mirroring
something huge. Pushing over HTTP is not supported (cgit is browse +
clone only); push over SSH to the NAS the usual way.

Left off on purpose: gravatar/libravatar-based avatars — those need the
container to fetch images from an external host per-request, which
breaks the zero-network-calls-at-runtime property everything else here
is built around.

See [`cgitrc(5)`](https://git.zx2c4.com/cgit/tree/cgitrc.5.txt) for every
option (auth filters, per-repo settings, etc).

## CI: build, smoke test, publish

`.github/workflows/docker-build.yml`, on every push to `main` (and on
`v*` tags):

1. **Builds** `linux/amd64` only — arm64 isn't needed for this NAS, and
   building Git from source under QEMU emulation is notably slower
   (~20-30 min vs under a minute) for no benefit here. If a future NAS
   needs it, add `docker/setup-qemu-action` back and set
   `platforms: linux/amd64,linux/arm64`.
2. **Smoke tests** the built image before anything is pushed — against a
   real fixture repository created on the fly (a README, a Python file,
   and a `cgit.desc` set via git config), so the whole runtime path is
   exercised, not just "lighttpd answers": repo discovery and
   `.git`-suffix stripping show up in the index, the `cgit.desc` proves
   `enable-git-config` works, the about page proves the md2html filter
   and its python deps work, a blob view proves the pygments
   source-filter works, an actual `git clone` over HTTP proves the
   clone endpoint works, `custom.css` being served proves the lighttpd
   rewrite whitelist is intact, a `Content-Encoding: gzip` response
   proves `mod_deflate` is live, and the image's own `HEALTHCHECK` must
   report `healthy`. A green `docker build` doesn't guarantee a working
   container — this pipeline hit that exact gap for real during
   development (see git log: a musl compat flag silently dropped
   between two `make` invocations, then a privilege-drop ordering bug
   in how lighttpd opens its log files) — so nothing reaches the
   registry without having actually run, and actually rendered, first.
3. **Publishes** to `ghcr.io/zopiya/cgit` with multiple tags for
   different needs:
   - `latest` — floating, current `main`.
   - `main` — same, explicit branch name.
   - `<date>-<sha>` (e.g. `20260726-85c44e1`) — a fixed, traceable tag
     unique per commit (uses the commit's own date, so re-running the
     workflow for the same commit doesn't produce a different tag).
     Pin to one of these for a reproducible deployment instead of
     riding `latest`.
   - `<version>` — only on `v*` tag pushes (semver).

The same pipeline also runs on a **weekly schedule** (Monday nights),
rebuilding the identical sources so Alpine package security fixes
(lighttpd, python3, ...) flow into `latest` even while this repo itself
is quiet. GitHub disables scheduled runs after 60 days of repo
inactivity — an occasional commit or manual `workflow_dispatch` re-arms
them. Action versions are kept current by Dependabot
(`.github/dependabot.yml`); image-level dependencies are deliberately
hand-pinned (below) rather than auto-bumped.

Pull on the NAS with:

```sh
docker pull ghcr.io/zopiya/cgit:latest
# or, pinned:
docker pull ghcr.io/zopiya/cgit:20260726-85c44e1
```

GHCR packages pushed via `GITHUB_TOKEN` default to **private** — either
make the package public in its GitHub settings, or `docker login ghcr.io`
on the NAS with a PAT that has `read:packages`.

(Workflow permissions for `GITHUB_TOKEN` were already flipped to
read+write for this repo — `packages: write` is needed to push —
via `gh api -X PUT repos/zopiya/cgit/actions/permissions/workflow -f default_workflow_permissions=write`,
since the repo default was read-only.)

## Upgrading

In the `Dockerfile`:

- **cgit**: bump `CGIT_VERSION` (e.g. `1.3.2`), then update `CGIT_COMMIT`
  to match — cgit source is fetched via `git clone --branch vX.Y.Z`
  rather than a tarball (see the fetch-stage comments for why), and the
  clone is verified against this exact commit as defense against a moved
  tag:
  ```sh
  git ls-remote https://git.zx2c4.com/cgit v<version>   # get the tag
  git clone --depth 1 --branch v<version> https://git.zx2c4.com/cgit /tmp/c
  git -C /tmp/c rev-parse HEAD                           # -> CGIT_COMMIT
  ```
  (v1.3.1 is an *annotated* tag, so `ls-remote`'s hash is the tag object,
  not the commit — always get `CGIT_COMMIT` from `rev-parse HEAD` after
  an actual clone+checkout, not from `ls-remote` directly.)
- **Git**: bump `GIT_VERSION` to match the `GIT_VER` value in the target
  cgit release's own `Makefile`
  (https://git.zx2c4.com/cgit/plain/Makefile?h=vX.Y.Z) — cgit links Git's
  object files directly into `cgit.cgi`, so a mismatch can fail to build.
  Recompute `GIT_SHA256` against kernel.org's own published checksums:
  ```sh
  curl -fsSL https://www.kernel.org/pub/software/scm/git/sha256sums.asc \
    | grep "git-<version>.tar.xz"
  ```
- **custom.css**: nothing to do on upgrade — it layers over upstream
  `cgit.css` rather than modifying it. If code views ever fall back to a
  default monospace font, diff the new upstream `cgit.css` for
  `monospace` selectors and re-sync the list in `custom.css`.

After bumping either, push to `main` — CI rebuilds, smoke tests, and
publishes automatically; nothing to run by hand.
