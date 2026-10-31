#!/usr/bin/env python3
"""CGI router: personal authentication, official Git backend and Git LFS Basic API."""

import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

from access import session as access_session
from common import RequestError, config, git, object_path, pointer, project_entries, repository, validate_scan
import lfs_token

JSON_TYPE = "application/vnd.git-lfs+json"
OUT = sys.stdout.buffer
IN = sys.stdin.buffer
HEADERS_SENT = False


class LfsBearer:
    def __init__(self, token):
        self.token = token


def headers(status=200, content_type=JSON_TYPE, **extra):
    global HEADERS_SENT
    HEADERS_SENT = True
    values = {"Content-Type": content_type, "Cache-Control": "no-store",
              "X-Content-Type-Options": "nosniff", **extra}
    OUT.write(f"Status: {status}\r\n".encode())
    for key, value in values.items():
        OUT.write(f"{key.replace('_', '-')}: {value}\r\n".encode())
    OUT.write(b"\r\n")


def respond(status, payload, **extra):
    data = json.dumps(payload).encode()
    headers(status, Content_Length=len(data), **extra)
    if os.environ.get("REQUEST_METHOD") != "HEAD":
        OUT.write(data)


def unauthorized():
    respond(401, {"message": "Authentication required"},
            WWW_Authenticate='Basic realm="cgit", charset="UTF-8"')
    raise SystemExit


def basic_credentials():
    supplied = os.environ.get("HTTP_AUTHORIZATION", "")
    if not supplied:
        return None
    if supplied.startswith("Bearer "):
        token = supplied[7:]
        if not token or any(c.isspace() for c in token):
            raise ValueError("Malformed bearer token")
        return LfsBearer(token)
    if not supplied.startswith("Basic "):
        raise ValueError("Unsupported authorization scheme")
    raw = base64.b64decode(supplied[6:], validate=True)
    user, password = raw.split(b":", 1)
    return user.decode("utf-8", "strict"), password


def request_principal(cfg):
    try:
        credentials = basic_credentials()
    except (UnicodeError, ValueError, binascii.Error):
        unauthorized()
    if cfg["auth_mode"] == "access":
        if credentials is None:
            return None
        if isinstance(credentials, LfsBearer):
            return credentials
        username, secret = credentials
        if not access_session(cfg).authenticate(username, secret):
            unauthorized()
        return username
    if credentials is None:
        if cfg["auth_mode"] == "private":
            unauthorized()
        return None
    if isinstance(credentials, LfsBearer):
        unauthorized()
    username, password = credentials
    try:
        secret = Path(cfg["password_file"]).read_bytes().rstrip(b"\r\n")
    except OSError as exc:
        raise RequestError(503, "Authentication configuration unavailable") from exc
    valid = (cfg["username"]
             and hmac.compare_digest(username.encode(), cfg["username"].encode())
             and hmac.compare_digest(password, secret))
    if not valid:
        unauthorized()
    return username


def authorization(cfg):
    if cfg["auth_mode"] != "access":
        return None
    return access_session(cfg)


def require_access(authorization_value, name, principal, operation):
    try:
        authorization_value.require(name, principal, operation)
    except RequestError as exc:
        if exc.status == 401:
            unauthorized()
        raise


def require_operation(cfg, name, credential, operation, surface):
    if isinstance(credential, LfsBearer):
        if cfg["auth_mode"] != "access" or surface != "lfs":
            unauthorized()
        try:
            principal = lfs_token.verify(
                cfg,
                credential.token,
                name[:-4] if name.endswith(".git") else name,
                "download" if operation == "R" else "upload",
                authorization(cfg).state["generation"],
            )
        except ValueError:
            unauthorized()
    else:
        principal = credential
    if cfg["auth_mode"] == "access":
        require_access(authorization(cfg), name, principal, operation)
    elif operation == "W":
        if not cfg["username"]:
            raise RequestError(403, "HTTP writes require a configured account")
        if principal is None:
            unauthorized()
    return principal


def length(limit):
    value = os.environ.get("CONTENT_LENGTH", "")
    if not value.isdecimal():
        raise RequestError(411, "Content-Length required")
    size = int(value)
    if size > limit:
        raise RequestError(413, "Request exceeds configured size limit")
    return size


def read_json():
    content_type = os.environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip()
    if content_type not in (JSON_TYPE, "application/json"):
        raise RequestError(415, "Expected Git LFS JSON")
    size = length(1024 * 1024)
    data = IN.read(size)
    if len(data) != size:
        raise RequestError(400, "Incomplete request")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise RequestError(400, "Invalid JSON") from None
    if not isinstance(value, dict):
        raise RequestError(422, "Expected a JSON object")
    return value


def origin(cfg):
    if cfg["base_url"]:
        return cfg["base_url"]
    host = os.environ.get("HTTP_HOST", "")
    if not re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?", host):
        raise RequestError(400, "Invalid Host header")
    # Deliberately ignore forwarded headers; CGIT_BASE_URL owns external addressing.
    return "http://" + host


def object_info(value, cfg):
    if not isinstance(value, dict):
        raise RequestError(422, "Invalid object")
    oid, size = value.get("oid"), value.get("size")
    if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-f]{64}", oid):
        raise RequestError(422, "Invalid SHA-256 object ID")
    if type(size) is not int or size < 0:
        raise RequestError(422, "Invalid object size")
    if size > cfg["max_size"]:
        raise RequestError(413, "Object exceeds configured size limit")
    return oid, size


def batch(cfg, name, principal):
    payload = read_json()
    operation = payload.get("operation")
    if operation not in ("upload", "download"):
        raise RequestError(422, "Unsupported operation")
    require_operation(cfg, name, principal, "W" if operation == "upload" else "R", "lfs")
    if payload.get("hash_algo", "sha256") != "sha256":
        raise RequestError(422, "Only SHA-256 is supported")
    transfers = payload.get("transfers", ["basic"])
    if not isinstance(transfers, list) or "basic" not in transfers:
        raise RequestError(422, "Only basic transfer is supported")
    objects = payload.get("objects")
    if not isinstance(objects, list) or len(objects) > 1000:
        raise RequestError(422, "Expected at most 1000 objects")
    action_header = (
        {"Authorization": "Bearer " + principal.token}
        if isinstance(principal, LfsBearer) else None)
    results = []
    for obj in objects:
        oid, size = object_info(obj, cfg)
        path = object_path(cfg, name, oid)
        item = dict(oid=oid, size=size, authenticated=action_header is not None)
        href = origin(cfg) + "/" + quote(name, safe="/") + "/info/lfs/objects/" + oid
        exists = path.is_file()
        if exists and path.stat().st_size != size:
            item["error"] = dict(code=422, message="Stored size does not match pointer")
        elif operation == "download":
            if exists:
                action = {"href": href}
                if action_header is not None:
                    action["header"] = action_header
                item["actions"] = {"download": action}
            else:
                item["error"] = dict(code=404, message="LFS object is missing")
        elif not exists:
            upload = {"href": href + f"?size={size}"}
            verify = {"href": href + "/verify"}
            if action_header is not None:
                upload["header"] = action_header
                verify["header"] = action_header
            item["actions"] = {"upload": upload, "verify": verify}
        results.append(item)
    respond(200, dict(transfer="basic", objects=results, hash_algo="sha256"))


def upload(cfg, name, oid, query, principal):
    require_operation(cfg, name, principal, "W", "lfs")
    size = length(cfg["max_size"])
    if query.get("size", [None]) != [str(size)]:
        raise RequestError(422, "Upload size differs from batch request")
    destination = object_path(cfg, name, oid)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".upload-", delete=False) as f:
            temp_path = Path(f.name)
            remaining = size
            digest = hashlib.sha256()
            while remaining:
                chunk = IN.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RequestError(400, "Incomplete upload")
                digest.update(chunk)
                f.write(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != oid:
                raise RequestError(422, "Upload SHA-256 does not match object ID")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, destination)
        # Persist the directory entry as well as the data before acknowledging.
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    respond(200, {})


def send_object(cfg, name, oid, filename="", inline=False, expected_size=None):
    path = object_path(cfg, name, oid)
    if not path.is_file():
        raise RequestError(404, "LFS object is missing; upload or migrate it first")
    with path.open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        if expected_size is not None and expected_size != size:
            raise RequestError(422, "Stored size does not match pointer")
        start, end, status = 0, size - 1, 200
        extra = {"Accept_Ranges": "bytes", "ETag": f'"{oid}"'}
        range_header = os.environ.get("HTTP_RANGE", "")
        if os.environ.get("HTTP_IF_RANGE", f'"{oid}"') != f'"{oid}"':
            range_header = ""
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()) or size == 0:
                headers(416, Content_Range=f"bytes */{size}", Content_Length=0)
                return
            a, b = match.groups()
            if a:
                start, end = int(a), min(int(b) if b else size - 1, size - 1)
            else:
                start = max(0, size - int(b))
            if start > end or start >= size:
                headers(416, Content_Range=f"bytes */{size}", Content_Length=0)
                return
            status = 206
            extra["Content_Range"] = f"bytes {start}-{end}/{size}"
        filename = filename.rsplit("/", 1)[-1]
        suffix = Path(filename).suffix.lower()
        # Active formats (HTML/SVG) are always attachments. PDF is sandboxed.
        types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf"}
        content_type = types.get(suffix, "application/octet-stream") if inline else "application/octet-stream"
        disposition = "inline" if inline and suffix in types else "attachment"
        extra["Content_Disposition"] = f"{disposition}; filename*=UTF-8''{quote(filename or oid, safe='')}"
        extra["Content_Security_Policy"] = "sandbox; default-src 'none'"
        headers(status, content_type, Content_Length=end - start + 1, **extra)
        if os.environ.get("REQUEST_METHOD") != "HEAD":
            f.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                OUT.write(chunk)
                remaining -= len(chunk)


def lfs(cfg, match, method, query, principal):
    _, name = repository(cfg, match[1])
    action = match[2]
    if action == "objects/batch" and method == "POST":
        batch(cfg, name, principal)
        return
    obj = re.fullmatch(r"objects/([0-9a-f]{64})(/verify)?", action)
    if obj:
        oid = obj[1]
        if obj[2] and method == "POST":
            require_operation(cfg, name, principal, "W", "lfs")
            requested, size = object_info(read_json(), cfg)
            path = object_path(cfg, name, oid)
            if requested != oid or not path.is_file() or path.stat().st_size != size:
                raise RequestError(422, "Object verification failed")
            respond(200, {})
            return
        if not obj[2]:
            if method == "PUT":
                upload(cfg, name, oid, query, principal)
                return
            if method in ("GET", "HEAD"):
                require_operation(cfg, name, principal, "R", "lfs")
                send_object(cfg, name, oid, query.get("filename", [""])[0],
                            query.get("inline") == ["1"])
                return
    if action.startswith("locks"):
        raise RequestError(501, "LFS locking is not supported by this personal server")
    raise RequestError(405, "Unsupported LFS endpoint or method")


def smart_http(cfg, match, method, query, principal):
    repo, name = repository(cfg, match[1])
    action = match[2]
    if action == "info/refs":
        if method != "GET" or query.get("service") not in (["git-upload-pack"], ["git-receive-pack"]):
            raise RequestError(400, "Invalid Git service request")
        service = query["service"][0]
    else:
        if method != "POST":
            raise RequestError(405, "Git RPC requires POST")
        service = action
    if service == "git-receive-pack":
        if not cfg["http_push"]:
            raise RequestError(403, "HTTP push is disabled; use SSH or enable CGIT_HTTP_PUSH")
        principal = require_operation(cfg, name, principal, "W", "git")
    else:
        principal = require_operation(cfg, name, principal, "R", "git")
    env = os.environ.copy()
    env.update(GIT_PROJECT_ROOT=cfg["repos"], GIT_HTTP_EXPORT_ALL="1",
               PATH_INFO=f"/{name}/{action}",
               GIT_PROTOCOL=env.get("HTTP_GIT_PROTOCOL", ""))
    if cfg["auth_mode"] == "access":
        root = str(authorization(cfg).root)
        bindir, libdir = authorization(cfg).gitolite_directories()
        logical_name = name[:-4] if name.endswith(".git") else name
        env.update(HOME=root, GITOLITE_HTTP_HOME=root,
                   GL_BINDIR=str(bindir), GL_LIBDIR=str(libdir),
                   GL_REPO=logical_name, GL_USER=principal or "anonymous",
                   REMOTE_USER=principal or "anonymous")
    elif cfg["username"] and env.get("HTTP_AUTHORIZATION"):
        env["REMOTE_USER"] = cfg["username"]
    os.execvpe("git", ["git", "-c", f"safe.directory={repo}", "-c",
                        f"http.receivepack={'true' if cfg['http_push'] else 'false'}",
                        "http-backend"], env)


def match_web_repository(authorization, value):
    value = value.strip("/")
    if not value:
        return None
    if any(part in (".", "..") or not part for part in value.split("/")):
        return None
    for logical in sorted(authorization.state["repositories"], key=len, reverse=True):
        for alias in (logical, logical + ".git"):
            if value == alias or value.startswith(alias + "/"):
                return logical
    return None


def web_target(cfg, path, query):
    authorization_value = authorization(cfg)
    if authorization_value is None:
        return None
    for key in ("r", "url"):
        values = query.get(key)
        if values is None:
            continue
        if len(values) != 1:
            raise RequestError(400, "Ambiguous repository URL")
        value = values[0]
        if key == "url":
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc:
                return None
            value = parsed.path
        return match_web_repository(authorization_value, value)
    return match_web_repository(authorization_value, path)


def sweep_policy_files(directory):
    now = time.time()
    entries = []
    for entry in directory.iterdir():
        if entry.is_symlink():
            entry.unlink()
            continue
        if not entry.is_file():
            continue
        try:
            age = now - entry.stat().st_mtime
        except FileNotFoundError:
            continue
        if age > 3600:
            entry.unlink(missing_ok=True)
        else:
            entries.append(entry)
    if len(entries) > 2048:
        for entry in sorted(entries, key=lambda item: item.stat().st_mtime)[:len(entries) - 2048]:
            entry.unlink(missing_ok=True)


def request_cgit_config(cfg, visible):
    runtime = Path(cfg["cgit_config"]).parent
    policies = runtime.parent / "policies"
    policies.mkdir(mode=0o700, parents=True, exist_ok=True)
    if policies.is_symlink():
        raise ValueError("Policy directory must not be a symlink")
    sweep_policy_files(policies)
    project_file = config_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=policies, prefix="projects-",
                                         delete=False) as stream:
            project_file = Path(stream.name)
            os.chmod(project_file, 0o600)
            for name in visible:
                stream.write(name + ".git\n")
        source = Path(cfg["cgit_config"]).read_text()
        source = re.sub(r"^project-list=.*\n?", "", source, flags=re.M)
        source, count = re.subn(r"^scan-path=", f"project-list={project_file}\nscan-path=",
                                source, count=1, flags=re.M)
        if count != 1:
            raise ValueError("Generated cgit configuration has no unique scan-path")
        with tempfile.NamedTemporaryFile(mode="w", dir=policies, prefix="cgitrc-",
                                         delete=False) as stream:
            config_file = Path(stream.name)
            os.chmod(config_file, 0o600)
            stream.write(source)
        return config_file
    except BaseException:
        if project_file is not None:
            project_file.unlink(missing_ok=True)
        if config_file is not None:
            config_file.unlink(missing_ok=True)
        raise


def visible_repositories(cfg, principal):
    readable = authorization(cfg).readable(principal)
    visible = []
    for logical in sorted(readable):
        try:
            repository(cfg, logical, serving=True)
        except RequestError as exc:
            if exc.status == 404:
                continue
            raise
        visible.append(logical)
    return visible


def plain_lfs(cfg, path, query, principal):
    if "/plain/" not in path:
        return False
    name, filename = path.lstrip("/").split("/plain/", 1)
    repo, name = repository(cfg, name)
    require_operation(cfg, name, principal, "R", "lfs")
    revision = query.get("id", query.get("h", ["HEAD"]))[0]
    result = git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}", check=False)
    if result.returncode:
        return False
    commit = result.stdout.decode().strip()
    spec = f"{commit}:{filename}"
    result = git(repo, "cat-file", "-s", spec, check=False)
    if result.returncode or int(result.stdout) > 1024:
        return False
    result = git(repo, "cat-file", "blob", spec, check=False)
    info = pointer(result.stdout) if not result.returncode else None
    if not info:
        return False
    send_object(cfg, name, info[0], filename, inline=True, expected_size=info[1])
    return True


def main():
    cfg = config()
    method = os.environ.get("REQUEST_METHOD", "GET")
    # REQUEST_URI survives lighttpd's rewrite; PATH_INFO is decoded by the server.
    url = urlsplit(os.environ.get("REQUEST_URI", "/"))
    try:
        path = unquote(url.path, errors="strict")
    except UnicodeError:
        raise RequestError(400, "Path must be UTF-8") from None
    if any(ord(c) < 32 or c == "\\" or ord(c) == 127 for c in path):
        raise RequestError(400, "Invalid path")
    if any(p in (".", "..") for p in path.split("/")):
        raise RequestError(404, "Not found")
    query = parse_qs(url.query, keep_blank_values=True)
    principal_name = request_principal(cfg)
    if path == "/_login":
        if cfg["auth_mode"] != "access":
            raise RequestError(404, "Not found")
        if method not in ("GET", "HEAD"):
            raise RequestError(405, "Method not allowed")
        if principal_name is None or isinstance(principal_name, LfsBearer):
            unauthorized()
        # No user-controlled redirect and no HTML response-body injection.
        headers(303, content_type="text/plain; charset=UTF-8", Location="/", Content_Length=0)
        return
    if path == "/_health":
        if method not in ("GET", "HEAD"):
            raise RequestError(405, "Method not allowed")
        if (cfg["auth_mode"] == "access"
                and (principal_name is None or isinstance(principal_name, LfsBearer))):
            unauthorized()
        healthy = (Path(cfg["repos"]).is_dir() and Path(cfg["lfs"]).is_dir()
                   and os.access(cfg["cgit"], os.X_OK))
        respond(200 if healthy else 503, {"status": "ok" if healthy else "unavailable"})
        return
    match = re.fullmatch(r"/(.+?)/info/lfs/(.*)", path)
    if match:
        lfs(cfg, match, method, query, principal_name)
        return
    match = re.fullmatch(r"/(.+?)/(info/refs|git-upload-pack|git-receive-pack)", path)
    if match and (match[2] != "info/refs" or "service" in query):
        smart_http(cfg, match, method, query, principal_name)
        return
    if method not in ("GET", "HEAD"):
        raise RequestError(405, "Method not allowed")
    if plain_lfs(cfg, path, query, principal_name):
        return
    # cgit performs its own scan (including legacy query-string URLs). Check
    # ambiguous flags and unsupported paths before *any* page, including index.
    validate_scan(cfg["repos"])
    projects = cfg.get("repository_policy", {}).get("project_list")
    if projects is not None:
        project_entries(projects)
    if cfg["auth_mode"] == "access":
        if isinstance(principal_name, LfsBearer):
            unauthorized()
        target = web_target(cfg, path, query)
        if target is not None:
            require_access(authorization(cfg), target, principal_name, "R")
        visible = visible_repositories(cfg, principal_name)
        cgit_config = request_cgit_config(cfg, visible)
        env = authorization(cfg).cgi_environment(principal_name, cgit_config)
    else:
        env = os.environ.copy()
        env["CGIT_CONFIG"] = cfg["cgit_config"]
    env.update(PATH_INFO=path, SCRIPT_NAME="", QUERY_STRING=url.query)
    os.execve(cfg["cgit"], [cfg["cgit"]], env)


if __name__ == "__main__":
    try:
        main()
    except RequestError as exc:
        respond(exc.status, {"message": exc.message})
    except BrokenPipeError:
        pass  # Client cancelled a streaming download.
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"cgit gateway: {type(exc).__name__}: {exc}", file=sys.stderr)
        if not HEADERS_SENT:
            respond(500, {"message": "Server error; check container logs and cgit-doctor"})
        sys.exit(1)
