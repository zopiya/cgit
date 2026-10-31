#!/usr/bin/env python3
"""Run scheduled maintenance jobs with durable state and cross-service write fencing."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit

from access import canonical as access_canonical
from access import validate_users as validate_access_users
from common import digest_file

JOB_ID = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
OID = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}\Z")
ACCESS_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]*(?:/[A-Za-z0-9][A-Za-z0-9._@+-]*)*\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
MAX_CONFIG = 1024 * 1024
TIMEZONE = re.compile(r"[A-Za-z0-9_+\-/]{1,64}\Z")
ENV_PASSTHROUGH = {
    "ALL_PROXY", "HOME", "HTTP_PROXY", "HTTPS_PROXY", "LANG", "LC_ALL", "NO_PROXY",
    "PATH", "SSH_AUTH_SOCK", "TMPDIR", "TZ", "all_proxy", "http_proxy", "https_proxy",
    "no_proxy",
}


class JobError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}-",
                                         delete=False, encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_config(path):
    path = Path(path)
    data = path.read_bytes()
    if len(data) > MAX_CONFIG:
        raise ValueError("Jobs configuration exceeds size limit")
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Unsupported jobs configuration")
    if set(value) - {"schema_version", "timezone", "jobs"}:
        raise ValueError("Unknown jobs configuration field")
    if (not isinstance(value.get("timezone"), str)
            or not TIMEZONE.fullmatch(value["timezone"])):
        raise ValueError("timezone is required")
    jobs = value.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list")
    seen = set()
    for job in jobs:
        validate_job(job)
        if job["id"] in seen:
            raise ValueError("Duplicate job ID")
        seen.add(job["id"])
    if not jobs:
        raise ValueError("At least one job is required")
    return value


def validate_job(job):
    if not isinstance(job, dict):
        raise ValueError("Job must be an object")
    required = {"id", "type", "cron", "enabled"}
    if not required.issubset(job):
        raise ValueError("Job requires id, type, cron and enabled")
    if not JOB_ID.fullmatch(job["id"]):
        raise ValueError("Invalid job ID")
    if type(job["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if not isinstance(job["cron"], str) or len(job["cron"].split()) not in (5, 6, 7):
        raise ValueError("cron must contain 5, 6 or 7 fields")
    if not isinstance(job.get("timeout", 3600), int) or not 1 <= job.get("timeout", 3600) <= 86400:
        raise ValueError("timeout must be between 1 second and one day")
    kind = job["type"]
    allowed = {
        "check": {"id", "type", "cron", "enabled", "timeout", "git", "lfs"},
        "archive": {"id", "type", "cron", "enabled", "timeout", "destination", "retain_count"},
        "backup": {"id", "type", "cron", "enabled", "timeout", "repository",
                   "password_file", "paths", "tags"},
        "sync-in": {"id", "type", "cron", "enabled", "timeout", "source",
                    "repository", "lfs", "allow_rewrite", "allow_delete"},
        "maintenance": {"id", "type", "cron", "enabled", "timeout", "script", "args"},
    }
    if kind not in allowed:
        raise ValueError(f"Unsupported job type: {kind}")
    unknown = set(job) - allowed[kind]
    if unknown:
        raise ValueError(f"Unknown {kind} job fields: {sorted(unknown)}")
    for field in ("git", "lfs", "allow_rewrite", "allow_delete"):
        if field in job and type(job[field]) is not bool:
            raise ValueError(f"{field} must be boolean")
    for field in ("retain_count",):
        if field in job and (type(job[field]) is not int or not 1 <= job[field] <= 10000):
            raise ValueError(f"{field} must be a positive bounded integer")
    for field in ("source", "repository", "destination", "password_file", "script"):
        if field in job and (not isinstance(job[field], str) or not job[field]
                             or any(ord(c) < 32 for c in job[field])):
            raise ValueError(f"{field} must be a nonempty string")
    if kind == "sync-in" and not isinstance(job.get("repository"), str):
        raise ValueError("sync-in requires repository")
    if kind == "maintenance":
        args = job.get("args", [])
        if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
            raise ValueError("maintenance args must be a string array")


def job_matches(config, job_id):
    for job in config["jobs"]:
        if job["id"] == job_id:
            return job
    raise ValueError("Unknown job ID")


def command(args, *, timeout, env=None, cwd=None, input_data=None):
    try:
        result = subprocess.run(
            args,
            input=input_data,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise JobError(f"Command timed out: {Path(args[0]).name}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()[:4000]
        raise JobError(f"{Path(args[0]).name} exited {result.returncode}: {detail}")
    return result


def git_environment():
    """Use repository-local state without inheriting shell or remote-helper injection."""
    env = {key: value for key, value in os.environ.items() if key in ENV_PASSTHROUGH}
    env.update(
        GIT_ALLOW_PROTOCOL="file:https:ssh",
        GIT_ASKPASS="false",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_LFS_SKIP_SMUDGE="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def validate_sync_source(source):
    """Accept only approved Git transport syntax; never pass shell-like helpers to Git."""
    if not isinstance(source, str) or not source or len(source) > 4096:
        raise JobError("Invalid sync source")
    if any(ord(c) < 32 or ord(c) == 127 for c in source):
        raise JobError("Sync source contains control characters")
    local = Path(source)
    if local.is_absolute():
        if local.is_symlink() or not local.exists():
            raise JobError("Local sync source is unavailable or a symlink")
        return source
    try:
        parsed = urlsplit(source)
        port = parsed.port
    except ValueError as exc:
        raise JobError("Invalid sync source URL") from exc
    scheme = parsed.scheme.lower()
    if scheme not in ("https", "ssh"):
        raise JobError("Sync source must be an absolute path, https:// or ssh:// URL")
    if not parsed.hostname or not parsed.path or parsed.path == "/":
        raise JobError("Sync source URL requires a host and repository path")
    if parsed.query or parsed.fragment or parsed.password is not None:
        raise JobError("Sync source URL must not contain credentials, query or fragment")
    if scheme == "https" and parsed.username is not None:
        raise JobError("HTTPS sync source must not contain user information")
    if port is not None and not 1 <= port <= 65535:
        raise JobError("Invalid sync source port")
    return source


@contextmanager
def write_fence(access_root, job_id, timeout):
    root = Path(access_root)
    if not root.is_absolute() or not root.is_dir():
        raise JobError("Access root is unavailable for write fencing")
    marker = root / "maintenance"
    try:
        fd_marker = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise JobError("Access maintenance is already active") from exc
    try:
        os.write(fd_marker, f"job={job_id}\n".encode())
        os.fsync(fd_marker)
    finally:
        os.close(fd_marker)
    lock_fd = None
    try:
        lock_fd = os.open(root / "write.lock", os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JobError("Timed out waiting for in-flight writes")
                time.sleep(.2)
        yield
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        marker.unlink(missing_ok=True)


def run_check(ctx, job, deadline):
    args = [sys.executable, str(Path(__file__).with_name("doctor.py"))]
    if job.get("git"):
        args.append("--git")
    if job.get("lfs"):
        args.append("--lfs")
    remaining = max(1, int(deadline - time.monotonic()))
    result = command(args, timeout=remaining, env={
        **os.environ,
        "CGIT_RUNTIME_CONFIG": str(ctx.runtime_config),
    })
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise JobError("doctor returned invalid JSON") from exc
    if not report.get("ok"):
        raise JobError("Integrity check failed: " + "; ".join(report.get("findings", []))[:4000])
    return {"repositories": len(report.get("repositories", []))}


def iter_archive(root):
    root = Path(root)
    for path in root.rglob("*"):
        if path.name == "write.lock" or path.name == "maintenance":
            continue
        if ".upload-" in path.name:
            continue
        yield path


def run_archive(ctx, job, deadline):
    destination = Path(job.get("destination", str(ctx.backups)))
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.resolve() == Path("/"):
        raise JobError("Archive destination is unsafe")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = destination / f"cgit-data-{stamp}-{job['id']}.tar.gz"
    partial = destination / f".{final.name}.partial"
    timeout = max(1, int(deadline - time.monotonic()))
    try:
        with write_fence(ctx.access, job["id"], timeout):
            with tarfile.open(partial, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                for label in ("repos", "lfs"):
                    root = Path(ctx.root[label])
                    if not root.is_dir():
                        raise JobError(f"{label} directory is unavailable")
                    archive.add(root, arcname=label, recursive=False)
                    for path in iter_archive(root):
                        if path.is_dir() and not path.is_symlink():
                            continue
                        archive.add(path, arcname=str(Path(label) / path.relative_to(root)),
                                    recursive=False)
            with partial.open("rb") as stream:
                archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                os.fsync(stream.fileno())
            os.replace(partial, final)
            directory_fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        manifest = {
            "schema_version": 1,
            "job_id": job["id"],
            "created": now(),
            "archive": final.name,
            "sha256": archive_hash,
            "bytes": final.stat().st_size,
            "roots": ["repos", "lfs"],
            "note": "Data archive; access policy, credentials and token keys are not included.",
        }
        atomic_json(final.with_suffix(final.suffix + ".manifest.json"), manifest)
        retain = job.get("retain_count")
        if retain:
            archives = sorted(destination.glob("cgit-data-*.tar.gz"))
            for old in archives[:-retain]:
                old.unlink()
                old.with_suffix(old.suffix + ".manifest.json").unlink(missing_ok=True)
        return {"archive": final.name, "sha256": archive_hash, "bytes": manifest["bytes"]}
    finally:
        partial.unlink(missing_ok=True)


def restic_base(job):
    repository = job["repository"]
    password_file = Path(job["password_file"])
    if "://" in repository:
        parsed = repository.split("://", 1)[1]
        if "@" in parsed.split("/", 1)[0] and ":" in parsed.split("@", 1)[0]:
            raise JobError("Credentials in repository URLs are not allowed")
    if not password_file.is_file():
        raise JobError("Restic password file is unavailable")
    return ["restic", "-r", repository, "--password-file", str(password_file)]


def run_backup(ctx, job, deadline):
    base = restic_base(job)
    paths = job.get("paths") or [
        str(ctx.root["repos"]), str(ctx.root["lfs"]), str(ctx.access)]
    absolute = [str(Path(path).resolve()) for path in paths]
    if any(not Path(path).exists() for path in paths):
        raise JobError("Backup path is unavailable")
    timeout = max(1, int(deadline - time.monotonic()))
    with write_fence(ctx.access, job["id"], timeout):
        repository = Path(job["repository"])
        if repository.is_absolute() and not (repository / "config").is_file():
            repository.mkdir(parents=True, exist_ok=True)
            command([*base, "init"], timeout=max(1, int(deadline - time.monotonic())))
        args = [
            *base, "backup", *absolute,
            "--json",
            "--exclude", "**/maintenance",
            "--exclude", "**/write.lock",
            "--exclude", "**/.upload-*",
            "--exclude", "**/.gitolite/logs/**",
        ]
        for tag in job.get("tags", []):
            args.extend(("--tag", tag))
        result = command(args, timeout=max(1, int(deadline - time.monotonic())))
    snapshot = None
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("message_type") == "summary":
            snapshot = event.get("snapshot_id")
    if not snapshot:
        raise JobError("Restic did not report a completed snapshot")
    return {"snapshot": snapshot, "paths": ["repos", "lfs", "access"]}


def parse_remote_refs(source, timeout, env):
    result = command(["git", "ls-remote", "--symref", source,
                      "HEAD", "refs/heads/*", "refs/tags/*"], timeout=timeout, env=env)
    refs, head = {}, None
    for raw in result.stdout.decode("utf-8", "replace").splitlines():
        if raw.startswith("ref: "):
            target, name = raw[5:].split("\t", 1)
            if name == "HEAD":
                head = target
            continue
        oid, name = raw.split("\t", 1)
        if name.endswith("^{}"):
            continue
        if not OID.fullmatch(oid):
            raise JobError("Remote returned an invalid object ID")
        refs[name] = oid
    return refs, head


def run_sync_in(ctx, job, deadline):
    source = validate_sync_source(job["source"])
    logical = job["repository"].strip("/")
    if not ACCESS_REPO.fullmatch(logical):
        raise JobError("Invalid sync source or repository")
    repo = ctx.root["repos"] / f"{logical}.git"
    if repo.is_symlink() or not repo.is_dir() or not (repo / "HEAD").is_file():
        raise JobError("Target bare repository is unavailable")
    timeout = max(1, int(deadline - time.monotonic()))
    env = git_environment()
    remote_refs, remote_head = parse_remote_refs(source, timeout, env)
    branches = {k[11:]: v for k, v in remote_refs.items() if k.startswith("refs/heads/")}
    tags = {k[10:]: v for k, v in remote_refs.items() if k.startswith("refs/tags/")}
    command(["git", "-c", f"safe.directory={repo}", "--git-dir", str(repo),
             "fetch", "--no-tags", "--force", source,
             "+refs/heads/*:refs/cgit-import/heads/*",
             "+refs/tags/*:refs/cgit-import/tags/*"],
            timeout=max(1, int(deadline - time.monotonic())), env=env)
    local = {}
    for line in command(["git", "--git-dir", str(repo), "for-each-ref",
                         "--format=%(refname)%09%(objectname)",
                         "refs/heads", "refs/tags"], timeout=60,
                        env=env).stdout.decode().splitlines():
        name, oid = line.split("\t", 1)
        local[name] = oid
    updates = []
    for name, oid in branches.items():
        old = local.get("refs/heads/" + name)
        if old and old != oid and not job.get("allow_rewrite"):
            ancestor = subprocess.run(
                ["git", "--git-dir", str(repo), "merge-base", "--is-ancestor", old, oid],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60, env=env)
            if ancestor.returncode:
                raise JobError(f"Non-fast-forward branch: {name}")
        updates.append(("refs/heads/" + name, oid, old))
    for name in sorted(set(k[11:] for k in local if k.startswith("refs/heads/")) - set(branches)):
        if not job.get("allow_delete"):
            raise JobError(f"Remote deleted branch: {name}")
        updates.append(("refs/heads/" + name, None, local["refs/heads/" + name]))
    for name, oid in tags.items():
        old = local.get("refs/tags/" + name)
        if old and old != oid and not job.get("allow_rewrite"):
            raise JobError(f"Remote tag changed: {name}")
        updates.append(("refs/tags/" + name, oid, old))
    for name in sorted(set(k[10:] for k in local if k.startswith("refs/tags/")) - set(tags)):
        if not job.get("allow_delete"):
            raise JobError(f"Remote deleted tag: {name}")
        updates.append(("refs/tags/" + name, None, local["refs/tags/" + name]))

    work = Path(ctx.work) / f"sync-{job['id']}-{os.getpid()}"
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    fetched_objects = []
    try:
        if job.get("lfs", True):
            command(["git", "clone", "--no-checkout", source, str(work)],
                    timeout=max(1, int(deadline - time.monotonic())), env=env)
            if (work / ".lfsconfig").exists():
                raise JobError("Remote .lfsconfig is not accepted")
            command(["git", "-C", str(work), "lfs", "fetch", "--all", "origin"],
                    timeout=max(1, int(deadline - time.monotonic())), env=env)
            listed = command(["git", "-C", str(work), "lfs", "ls-files",
                              "--all", "--json"],
                             timeout=max(1, int(deadline - time.monotonic())), env=env)
            entries = json.loads(listed.stdout or b"[]")
            seen = set()
            for item in entries:
                oid, size = item.get("oid"), item.get("size")
                if (not isinstance(oid, str) or not re.fullmatch(r"[a-f0-9]{64}", oid)
                        or type(size) is not int or size < 0 or oid in seen):
                    raise JobError("Invalid remote LFS inventory")
                seen.add(oid)
                source_object = work / ".git/lfs/objects" / oid[:2] / oid[2:4] / oid
                if not source_object.is_file() or source_object.stat().st_size != size:
                    raise JobError(f"Remote LFS object missing or wrong size: {oid}")
                fetched_objects.append((oid, size, source_object))
        with write_fence(ctx.access, job["id"], max(1, int(deadline - time.monotonic()))):
            for oid, size, source_object in fetched_objects:
                destination = ctx.root["lfs"] / logical / "objects" / oid[:2] / oid[2:4] / oid
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_file() and destination.stat().st_size == size:
                    continue
                temporary = destination.with_name(f".upload-{os.getpid()}-{oid}")
                digest = hashlib.sha256()
                with source_object.open("rb") as source_stream, temporary.open("wb") as target_stream:
                    while chunk := source_stream.read(1024 * 1024):
                        digest.update(chunk)
                        target_stream.write(chunk)
                    target_stream.flush()
                    os.fsync(target_stream.fileno())
                if digest.hexdigest() != oid or temporary.stat().st_size != size:
                    raise JobError(f"Remote LFS object hash mismatch: {oid}")
                os.chmod(temporary, 0o644)
                os.replace(temporary, destination)
            object_format = command(
                ["git", "--git-dir", str(repo), "rev-parse", "--show-object-format"],
                timeout=30, env=env).stdout.strip()
            zero = "0" * (64 if object_format == b"sha256" else 40)
            lines = ["start"]
            for name, new, old in updates:
                lines.append(f"update {name} {new or zero} {old or zero}")
            lines.extend(("prepare", "commit"))
            command(["git", "--git-dir", str(repo), "update-ref", "--stdin"],
                    timeout=max(1, int(deadline - time.monotonic())),
                    env=env,
                    input_data=("\n".join(lines) + "\n").encode())
            if remote_head and remote_head in {f"refs/heads/{name}" for name in branches}:
                command(["git", "--git-dir", str(repo), "symbolic-ref", "HEAD", remote_head],
                        timeout=30, env=env)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"refs_updated": len(updates), "lfs_objects": len(fetched_objects)}


def run_maintenance(ctx, job, deadline):
    script = Path(job["script"])
    if not script.is_absolute() or not script.is_file() or script.is_symlink():
        raise JobError("Maintenance script is unavailable")
    with write_fence(ctx.access, job["id"], max(1, int(deadline - time.monotonic()))):
        command([str(script), *job.get("args", [])],
                timeout=max(1, int(deadline - time.monotonic())),
                cwd=ctx.work,
                env={**os.environ, "CGIT_REPOS": str(ctx.root["repos"]),
                     "CGIT_LFS": str(ctx.root["lfs"]), "CGIT_ACCESS": str(ctx.access)})
    return {"script": script.name}


def execute(ctx, job, deadline):
    return {
        "check": run_check,
        "archive": run_archive,
        "backup": run_backup,
        "sync-in": run_sync_in,
        "maintenance": run_maintenance,
    }[job["type"]](ctx, job, deadline)


def run_job(ctx, job):
    ctx.state.mkdir(parents=True, exist_ok=True)
    lock_path = ctx.state / "runner.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JobError("Another maintenance job is already running") from exc
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
        state_path = ctx.state / f"{job['id']}.json"
        previous = {}
        if state_path.is_file():
            try:
                previous = json.loads(state_path.read_text())
            except ValueError:
                previous = {}
        started = time.monotonic()
        state = {"schema_version": 1, "job_id": job["id"], "run_id": run_id,
                 "status": "running", "started": now(),
                 "last_success": previous.get("last_success")}
        atomic_json(state_path, state)
        deadline = started + job.get("timeout", 3600)

        def timed_out(signum, frame):
            raise JobError("Job exceeded configured timeout")

        previous_handler = signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, max(.1, deadline - time.monotonic()))
        try:
            summary = execute(ctx, job, deadline)
        except BaseException as exc:
            state.update(status="failed", finished=now(),
                         seconds=round(time.monotonic() - started, 3),
                         error=str(exc)[:4000])
            atomic_json(state_path, state)
            raise
        else:
            state.update(status="success", finished=now(),
                         seconds=round(time.monotonic() - started, 3),
                         last_success=now(), summary=summary)
            atomic_json(state_path, state)
            return state
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def restored_directory(root, name):
    path = Path(root) / name
    if path.is_symlink() or not path.is_dir():
        raise JobError(f"Restored tree is missing a safe {name} directory")
    return path


def verify_restored_access(root, repos):
    """Validate the restored identity snapshot without needing a live Gitolite binary."""
    access = restored_directory(root, "access")
    identity = access / "identity.json"
    if identity.is_symlink() or not identity.is_file() or identity.stat().st_size > MAX_CONFIG:
        raise JobError("Restored access snapshot is unavailable")
    state = json.loads(identity.read_text())
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise JobError("Restored access snapshot has an unsupported schema")
    generation = state.get("generation")
    unsigned = {key: value for key, value in state.items() if key != "generation"}
    if (not isinstance(generation, str) or not HASH.fullmatch(generation)
            or hashlib.sha256(access_canonical(unsigned)).hexdigest() != generation):
        raise JobError("Restored access snapshot fingerprint is invalid")
    validate_access_users(state.get("users"))
    manifest = state.get("repositories")
    if not isinstance(manifest, dict):
        raise JobError("Restored repository access manifest is invalid")
    common = access / ".gitolite/conf/gitolite.conf-compiled.pm"
    expected_common = state.get("common_digest")
    if (common.is_symlink() or not common.is_file() or not isinstance(expected_common, str)
            or not HASH.fullmatch(expected_common) or digest_file(common) != expected_common):
        raise JobError("Restored compiled access policy is invalid")
    for logical, expected in manifest.items():
        if (not isinstance(logical, str) or not ACCESS_REPO.fullmatch(logical)
                or logical == "gitolite-admin" or not isinstance(expected, str)
                or not HASH.fullmatch(expected)):
            raise JobError("Restored repository access entry is invalid")
        path = repos
        parts = logical.split("/")
        for part in parts[:-1]:
            path = path / part
            if path.is_symlink():
                raise JobError("Restored repository path contains a symlink")
        repo = path / (parts[-1] + ".git")
        if repo.is_symlink() or not repo.is_dir():
            raise JobError(f"Restored repository is missing: {logical}")
        fragment = repo / "gl-conf"
        if (fragment.is_symlink() or not fragment.is_file()
                or digest_file(fragment) != expected):
            raise JobError(f"Restored repository access fragment is invalid: {logical}")
    authorized = access / "ssh/authorized_keys"
    if authorized.is_symlink() or not authorized.is_file() or not authorized.stat().st_size:
        raise JobError("Restored SSH authorization is missing")
    token_key = access / "lfs-token.key"
    if token_key.is_symlink() or not token_key.is_file() or token_key.stat().st_size != 32:
        raise JobError("Restored LFS token key is invalid")
    return len(manifest)


def verify_restored_tree(destination, timeout, deadline):
    repos = restored_directory(destination, "repos")
    lfs = restored_directory(destination, "lfs")
    repository_count = verify_restored_access(destination, repos)
    settings = {
        "repos": str(repos),
        "lfs": str(lfs),
        "base_url": "",
        "auth_mode": "public",
        "username": "",
        "password_file": "",
        "http_push": False,
        "ssh_push": False,
    }
    # A restore target is often a volume directly under /. Its parent need
    # not be writable by the unprivileged maintenance user.
    with tempfile.TemporaryDirectory(prefix="cgit-restore-check-") as temporary:
        runtime_config = Path(temporary) / "settings.json"
        runtime_config.write_text(json.dumps(settings))
        remaining = max(1, min(timeout, int(deadline - time.monotonic())))
        result = command(
            [sys.executable, str(Path(__file__).with_name("doctor.py")), "--git", "--lfs"],
            timeout=remaining,
            env={**git_environment(), "CGIT_RUNTIME_CONFIG": str(runtime_config)},
        )
    report = json.loads(result.stdout)
    if report.get("ok") is not True:
        raise JobError("Restored Git/LFS integrity check failed: "
                       + "; ".join(report.get("findings", []))[:4000])
    checked_repositories = set(report.get("repositories", []))
    if len(checked_repositories) != repository_count:
        raise JobError("Restored repository inventory does not match the access snapshot")
    return {"repositories": repository_count}


def restore(args):
    destination = args.destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Restore destination must be absent or empty")
    if destination == Path("/") or destination.is_symlink():
        raise ValueError("Unsafe restore destination")
    destination.mkdir(parents=True, exist_ok=True)
    base = restic_base({
        "repository": args.repository,
        "password_file": str(args.password_file),
    })
    deadline = time.monotonic() + args.timeout
    command([*base, "restore", args.snapshot, "--target", str(destination), "--verify"],
            timeout=max(1, int(deadline - time.monotonic())))
    verified = verify_restored_tree(destination, args.timeout, deadline)
    return {"destination": str(destination), "snapshot": args.snapshot, "verified": verified}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/jobs/jobs.json"))
    parser.add_argument("--state", type=Path, default=Path("/job-state"))
    parser.add_argument("--work", type=Path, default=Path("/job-work"))
    parser.add_argument("--repos", type=Path, default=Path("/repos"))
    parser.add_argument("--lfs", type=Path, default=Path("/lfs"))
    parser.add_argument("--access", type=Path, default=Path("/access"))
    parser.add_argument("--backups", type=Path, default=Path("/backups"))
    parser.add_argument("--runtime-config", type=Path,
                        default=Path("/var/cache/cgit/runtime/settings.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    run = commands.add_parser("run")
    run.add_argument("--id", required=True)
    commands.add_parser("status")
    cron = commands.add_parser("crontab")
    cron.add_argument("--output", type=Path, required=True)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--repository", required=True)
    restore_parser.add_argument("--password-file", type=Path, required=True)
    restore_parser.add_argument("--snapshot", default="latest")
    restore_parser.add_argument("--destination", type=Path, required=True)
    restore_parser.add_argument("--timeout", type=int, default=86400)
    args = parser.parse_args()
    cfg = load_config(args.config)
    ctx = type("Context", (), {})()
    ctx.root = {"repos": args.repos, "lfs": args.lfs}
    ctx.access, ctx.backups = args.access, args.backups
    ctx.state, ctx.work = args.state, args.work
    ctx.runtime_config = args.runtime_config
    if args.command == "validate":
        print(json.dumps({"ok": True, "jobs": len(cfg["jobs"])}))
    elif args.command == "status":
        states = {}
        for job in cfg["jobs"]:
            path = args.state / f"{job['id']}.json"
            states[job["id"]] = json.loads(path.read_text()) if path.is_file() else None
        print(json.dumps({"ok": True, "jobs": states}, indent=2))
    elif args.command == "crontab":
        lines = [f"TZ={cfg['timezone']}"]
        for job in cfg["jobs"]:
            if job["enabled"]:
                lines.append(
                    f"{job['cron']} {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
                    f"--config {shlex.quote(str(args.config))} --state {shlex.quote(str(args.state))} "
                    f"--work {shlex.quote(str(args.work))} --repos {shlex.quote(str(args.repos))} "
                    f"--lfs {shlex.quote(str(args.lfs))} --access {shlex.quote(str(args.access))} "
                    f"--backups {shlex.quote(str(args.backups))} "
                    f"--runtime-config {shlex.quote(str(args.runtime_config))} "
                    f"run --id {shlex.quote(job['id'])}")
        args.output.write_text("\n".join(lines) + "\n")
        print(json.dumps({"ok": True, "scheduled": len(lines) - 1, "output": str(args.output)}))
    elif args.command == "run":
        state = run_job(ctx, job_matches(cfg, args.id))
        print(json.dumps(state, indent=2))
    else:
        print(json.dumps(restore(args), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (JobError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"maintenance job error: {exc}", file=sys.stderr)
        sys.exit(1)
