# cgit Docker deployment

Builds [cgit](https://git.zx2c4.com/cgit/) `v1.3.1` from source against the
exact Git release it's pinned to (`2.54.0`), served by lighttpd on port
`8080`. Nothing else — no reverse proxy, no TLS, no domain baked in.

## Design: self-contained, no external dependencies

This is meant to be a source of truth for GitOps repos (possibly including
the config that deploys other infrastructure), so it must never end up
gated behind something it might itself be used to bootstrap:

- `cgit.cgi` makes zero network calls at runtime. Fetching the cgit/Git
  source from git.zx2c4.com/kernel.org only happens once, at `docker build`
  time, and every download is checksum-verified (see `Dockerfile`) — the
  running container never phones home to anything.
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
maintain.

- `PUID`/`PGID` — uid/gid lighttpd runs as inside the container, so it can
  read repos owned by your NAS user. The entrypoint reuses an existing
  account at that id if one already exists in the image, so uncommon ids
  (e.g. a NAS's own admin uid) won't collide/fail.
- `TZ` — optional, e.g. `TZ=Asia/Shanghai`, for correctly localized
  timestamps in logs and in cgit's own UI. Defaults to UTC.

Open `http://<nas-lan-ip>:8080/` or `http://<nas-tailscale-ip>:8080/` —
both work identically, it's the same bound port either way.

A `HEALTHCHECK` is built into the image (`docker ps` / `docker inspect`
will show container health), and lighttpd logs requests to stdout and
errors to stderr, so `docker logs cgit` shows everything.

## Configuration

- `config/cgitrc` — baked into the image at `/etc/cgitrc`. Override at
  runtime by bind-mounting your own file over `/etc/cgitrc`.
- `config/lighttpd.conf` — CGI routing; shouldn't need changes for normal
  use. `clone-url` in `cgitrc` derives from the request's `Host` header
  rather than a hardcoded name, so `git clone` URLs shown in the UI are
  correct whether you're hitting the NAS by LAN IP, Tailscale IP, or (if
  you later add one) a reverse-proxied domain.

See [`cgitrc(5)`](https://git.zx2c4.com/cgit/tree/cgitrc.5.txt) for every
option (auth filters, syntax highlighting, per-repo settings, etc).

## CI

`.github/workflows/docker-build.yml` builds `linux/amd64` + `linux/arm64`
(covers both x86 and ARM-based NAS/SBC hardware) and pushes to
`ghcr.io/<owner>/<repo>` on every push to `main` and on version tags
(`v*`). Enable "Read and write permissions" for Actions under repo
Settings → Actions → General if the push step gets a 403. On your NAS:

```sh
docker pull ghcr.io/<owner>/<repo>:latest
```

## Upgrading

Bump `CGIT_VERSION` / `GIT_VERSION` build args in the `Dockerfile`, and
recompute `CGIT_SHA256` / `GIT_SHA256` to match:

```sh
curl -fsSL https://git.zx2c4.com/cgit/snapshot/cgit-<version>.tar.xz | sha256sum
curl -fsSL https://www.kernel.org/pub/software/scm/git/git-<version>.tar.xz | sha256sum
```

Keep `GIT_VERSION` matching the `GIT_VER` value in the target cgit
release's own `Makefile`
(https://git.zx2c4.com/cgit/plain/Makefile?h=vX.Y.Z) — cgit links Git's
object files directly into `cgit.cgi`, so a mismatch can fail to build.
