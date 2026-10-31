package runtime

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type Job struct {
	ID           string   `json:"id"`
	Type         string   `json:"type"`
	Cron         string   `json:"cron"`
	Enabled      bool     `json:"enabled"`
	Timeout      int      `json:"timeout,omitempty"`
	Git          *bool    `json:"git,omitempty"`
	LFS          *bool    `json:"lfs,omitempty"`
	Destination  string   `json:"destination,omitempty"`
	RetainCount  int      `json:"retain_count,omitempty"`
	Repository   string   `json:"repository,omitempty"`
	PasswordFile string   `json:"password_file,omitempty"`
	Paths        []string `json:"paths,omitempty"`
	Tags         []string `json:"tags,omitempty"`
	Source       string   `json:"source,omitempty"`
	AllowRewrite bool     `json:"allow_rewrite,omitempty"`
	AllowDelete  bool     `json:"allow_delete,omitempty"`
	Script       string   `json:"script,omitempty"`
	Args         []string `json:"args,omitempty"`
}
type JobsConfig struct {
	Schema   int    `json:"schema_version"`
	Timezone string `json:"timezone"`
	Jobs     []Job  `json:"jobs"`
}
type JobContext struct{ Config, State, Work, Repos, LFS, Access, Backups, RuntimeConfig string }

func boolDefault(v *bool, d bool) bool {
	if v == nil {
		return d
	}
	return *v
}
func loadJobs(p string) (JobsConfig, error) {
	var cfg JobsConfig
	var obj map[string]json.RawMessage
	if e := readJSON(p, &obj); e != nil {
		return cfg, e
	}
	for k := range obj {
		if !contains([]string{"schema_version", "timezone", "jobs"}, k) {
			return cfg, fmt.Errorf("unknown jobs config field")
		}
	}
	if e := json.Unmarshal(jsonBytes(obj), &cfg); e != nil {
		return cfg, e
	}
	if cfg.Schema != 1 || len(cfg.Jobs) == 0 {
		return cfg, fmt.Errorf("unsupported or empty jobs configuration")
	}
	if _, e := time.LoadLocation(cfg.Timezone); e != nil {
		return cfg, e
	}
	var raw []map[string]json.RawMessage
	if e := json.Unmarshal(obj["jobs"], &raw); e != nil {
		return cfg, e
	}
	seen := map[string]bool{}
	common := []string{"id", "type", "cron", "enabled", "timeout"}
	allowed := map[string][]string{"check": {"git", "lfs"}, "archive": {"destination", "retain_count"}, "backup": {"repository", "password_file", "paths", "tags"}, "sync-in": {"source", "repository", "lfs", "allow_rewrite", "allow_delete"}, "maintenance": {"script", "args"}}
	for i, j := range cfg.Jobs {
		if !regexp.MustCompile(`^[a-z][a-z0-9._\-]{0,63}$`).MatchString(j.ID) || seen[j.ID] {
			return cfg, fmt.Errorf("invalid or duplicate job ID")
		}
		seen[j.ID] = true
		fields, ok := allowed[j.Type]
		if !ok {
			return cfg, fmt.Errorf("unsupported job type")
		}
		for _, k := range []string{"id", "type", "cron", "enabled"} {
			if _, ok := raw[i][k]; !ok {
				return cfg, fmt.Errorf("missing job field %s", k)
			}
		}
		for k := range raw[i] {
			if !contains(common, k) && !contains(fields, k) {
				return cfg, fmt.Errorf("unknown %s field: %s", j.Type, k)
			}
		}
		cron := strings.Fields(j.Cron)
		if len(cron) < 5 || len(cron) > 7 || strings.ContainsAny(j.Cron, "\r\n") || !regexp.MustCompile(`^[0-9A-Za-z*/?,\-\s]+$`).MatchString(j.Cron) {
			return cfg, fmt.Errorf("invalid cron expression")
		}
		for _, k := range []string{"enabled", "git", "lfs", "allow_rewrite", "allow_delete"} {
			if v, ok := raw[i][k]; ok && string(v) != "true" && string(v) != "false" {
				return cfg, fmt.Errorf("%s must be boolean", k)
			}
		}
		if _, provided := raw[i]["timeout"]; !provided {
			j.Timeout = 3600
		} else if j.Timeout == 0 {
			return cfg, fmt.Errorf("timeout must be positive")
		}
		if _, provided := raw[i]["retain_count"]; provided && j.RetainCount == 0 {
			return cfg, fmt.Errorf("retain_count must be positive")
		}
		values := map[string]string{"source": j.Source, "repository": j.Repository, "password_file": j.PasswordFile, "destination": j.Destination, "script": j.Script}
		for k, v := range values {
			if _, provided := raw[i][k]; provided && v == "" {
				return cfg, fmt.Errorf("%s must be a nonempty string", k)
			}
		}
		if j.Timeout < 1 || j.Timeout > 86400 || j.RetainCount < 0 || j.RetainCount > 10000 {
			return cfg, fmt.Errorf("invalid timeout/retention")
		}
		for _, v := range []string{j.Source, j.Repository, j.PasswordFile, j.Destination, j.Script} {
			if strings.IndexFunc(v, func(r rune) bool { return r < 32 }) >= 0 {
				return cfg, fmt.Errorf("control character in job configuration")
			}
		}
		if j.Type == "sync-in" && (j.Source == "" || !repoRE.MatchString(j.Repository)) {
			return cfg, fmt.Errorf("sync-in requires canonical repository and source")
		}
		cfg.Jobs[i] = j
	}
	return cfg, nil
}
func exclusiveFence(ctx context.Context, root, label string, keepFailure bool) (func(bool), error) {
	if !filepath.IsAbs(root) || !isDir(root) {
		return nil, fmt.Errorf("write fence root unavailable")
	}
	marker := filepath.Join(root, "maintenance")
	f, e := os.OpenFile(marker, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return nil, fmt.Errorf("maintenance already active or unavailable: %w", e)
	}
	_, e = f.WriteString(label + "\n")
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e != nil {
		return nil, e
	}
	lockpath := filepath.Join(root, "write.lock")
	if symlink(lockpath) {
		return nil, fmt.Errorf("unsafe write lock")
	}
	lock, e := os.OpenFile(lockpath, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		if !keepFailure {
			os.Remove(marker)
		}
		return nil, e
	}
	for {
		e = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if e == nil {
			break
		}
		if e != syscall.EWOULDBLOCK && e != syscall.EAGAIN {
			lock.Close()
			return nil, e
		}
		select {
		case <-ctx.Done():
			lock.Close()
			if !keepFailure {
				os.Remove(marker)
			}
			return nil, ctx.Err()
		case <-time.After(100 * time.Millisecond):
		}
	}
	return func(success bool) {
		if success || !keepFailure {
			os.Remove(marker)
		}
		lock.Close()
	}, nil
}
func jobEnv() []string {
	env := cleanEnv(envDefault("HOME", "/job-work"))
	for _, k := range []string{"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSH_AUTH_SOCK", "TMPDIR"} {
		if v := os.Getenv(k); v != "" {
			env = setEnv(env, k, v)
		}
	}
	return append(env, "GIT_ALLOW_PROTOCOL=file:https:ssh", "GIT_ASKPASS=false", "GIT_LFS_SKIP_SMUDGE=1")
}
func validateSource(source string) error {
	if source == "" || strings.IndexFunc(source, func(r rune) bool { return r <= 32 || r == 127 }) >= 0 {
		return fmt.Errorf("Sync source must be an absolute local path or HTTPS/SSH URL")
	}
	if filepath.IsAbs(source) {
		if !isDir(source) || symlink(source) {
			return fmt.Errorf("source directory unavailable")
		}
		return nil
	}
	u, e := url.Parse(source)
	if e != nil || !contains([]string{"https", "ssh"}, u.Scheme) || u.Hostname() == "" || u.RawQuery != "" || u.Fragment != "" || u.Path == "" {
		return fmt.Errorf("Sync source must be an absolute local path or HTTPS/SSH URL")
	}
	if u.User != nil {
		if _, ok := u.User.Password(); ok || u.Scheme != "ssh" {
			return fmt.Errorf("embedded credentials forbidden")
		}
	}
	return nil
}
func archive(ctx context.Context, c JobContext, j Job) (map[string]any, error) {
	destination := j.Destination
	if destination == "" {
		destination = c.Backups
	}
	if symlink(destination) || filepath.Clean(destination) == "/" {
		return nil, fmt.Errorf("unsafe archive destination")
	}
	if e := os.MkdirAll(destination, 0700); e != nil {
		return nil, e
	}
	name := fmt.Sprintf("cgit-data-%s-%s.tar.gz", time.Now().UTC().Format("20060102T150405Z"), j.ID)
	final := filepath.Join(destination, name)
	f, e := os.CreateTemp(destination, ".archive-*.partial")
	if e != nil {
		return nil, e
	}
	defer os.Remove(f.Name())
	defer f.Close()
	release, e := exclusiveFence(ctx, jobWriteRoot(c), j.ID, false)
	if e != nil {
		return nil, e
	}
	defer release(true)
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)
	for label, root := range map[string]string{"repos": c.Repos, "lfs": c.LFS} {
		e = filepath.WalkDir(root, func(p string, d os.DirEntry, e error) error {
			if e != nil {
				return e
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			default:
			}
			if d.Name() == "maintenance" || d.Name() == "write.lock" || strings.Contains(d.Name(), ".upload-") {
				if d.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
			rel, e := filepath.Rel(root, p)
			if e != nil {
				return e
			}
			st, e := os.Lstat(p)
			if e != nil {
				return e
			}
			target := ""
			if st.Mode()&os.ModeSymlink != 0 {
				target, e = os.Readlink(p)
				if e != nil {
					return e
				}
			}
			h, e := tar.FileInfoHeader(st, target)
			if e != nil {
				return e
			}
			h.Name = filepath.ToSlash(filepath.Join(label, rel))
			if e = tw.WriteHeader(h); e != nil {
				return e
			}
			if st.Mode().IsRegular() {
				s, e := os.Open(p)
				if e != nil {
					return e
				}
				_, e = io.Copy(tw, s)
				ce := s.Close()
				if e != nil {
					return e
				}
				return ce
			}
			return nil
		})
		if e != nil {
			return nil, e
		}
	}
	if e = tw.Close(); e != nil {
		return nil, e
	}
	if e = gz.Close(); e != nil {
		return nil, e
	}
	if e = f.Sync(); e != nil {
		return nil, e
	}
	if e = f.Close(); e != nil {
		return nil, e
	}
	h, e := digest(f.Name())
	if e != nil {
		return nil, e
	}
	st, e := os.Stat(f.Name())
	if e != nil {
		return nil, e
	}
	if e = os.Rename(f.Name(), final); e != nil {
		return nil, e
	}
	if e = syncDir(destination); e != nil {
		return nil, e
	}
	manifest := map[string]any{"schema_version": 1, "job_id": j.ID, "created": now(), "archive": name, "sha256": h, "bytes": st.Size(), "roots": []string{"repos", "lfs"}, "note": "Data archive; access policy, credentials and token keys are not included."}
	if e = atomicWrite(final+".manifest.json", jsonBytes(manifest), 0600); e != nil {
		return nil, e
	}
	if j.RetainCount > 0 {
		files, e := filepath.Glob(filepath.Join(destination, "cgit-data-*.tar.gz"))
		if e != nil {
			return nil, e
		}
		sort.Strings(files)
		for i := 0; i < len(files)-j.RetainCount; i++ {
			if e = os.Remove(files[i]); e != nil {
				return nil, e
			}
			if e = os.Remove(files[i] + ".manifest.json"); e != nil && !os.IsNotExist(e) {
				return nil, e
			}
		}
	}
	return map[string]any{"archive": name, "sha256": h, "bytes": st.Size()}, nil
}
func resticArgs(repository, password string) ([]string, error) {
	if repository == "" || !filepath.IsAbs(password) || !regular(password) || symlink(password) {
		return nil, fmt.Errorf("restic repository/password file unavailable")
	}
	return []string{"-r", repository, "--password-file", password}, nil
}
func backup(ctx context.Context, c JobContext, j Job) (map[string]any, error) {
	base, e := resticArgs(j.Repository, j.PasswordFile)
	if e != nil {
		return nil, e
	}
	paths := j.Paths
	if len(paths) == 0 {
		paths = []string{c.Repos, c.LFS, c.Access}
	}
	for i, p := range paths {
		if !exists(p) {
			return nil, fmt.Errorf("backup path unavailable")
		}
		paths[i], e = filepath.EvalSymlinks(p)
		if e != nil {
			return nil, e
		}
	}
	release, e := exclusiveFence(ctx, jobWriteRoot(c), j.ID, false)
	if e != nil {
		return nil, e
	}
	defer release(true)
	if filepath.IsAbs(j.Repository) && !regular(filepath.Join(j.Repository, "config")) {
		if e = os.MkdirAll(j.Repository, 0700); e != nil {
			return nil, e
		}
		if _, e = command(ctx, resticEnv(), nil, "restic", append(append([]string{}, base...), "init")...); e != nil {
			return nil, e
		}
	}
	args := append(append([]string{}, base...), "backup")
	args = append(args, paths...)
	args = append(args, "--json", "--exclude", "**/maintenance", "--exclude", "**/write.lock", "--exclude", "**/.upload-*", "--exclude", "**/.gitolite/logs/**")
	for _, tag := range j.Tags {
		args = append(args, "--tag", tag)
	}
	b, e := command(ctx, resticEnv(), nil, "restic", args...)
	if e != nil {
		return nil, e
	}
	snapshot := ""
	for _, line := range strings.Split(string(b), "\n") {
		var v map[string]any
		if json.Unmarshal([]byte(line), &v) == nil && v["message_type"] == "summary" {
			snapshot, _ = v["snapshot_id"].(string)
		}
	}
	if snapshot == "" {
		return nil, fmt.Errorf("restic did not report a complete snapshot")
	}
	return map[string]any{"snapshot": snapshot, "paths": []string{"repos", "lfs", "access"}}, nil
}
func now() string { return time.Now().UTC().Format(time.RFC3339) }
func readRefs(ctx context.Context, repo string) (map[string]string, error) {
	b, e := command(ctx, jobEnv(), nil, "git", "--git-dir", repo, "for-each-ref", "--format=%(refname)%09%(objectname)", "refs/heads", "refs/tags")
	if e != nil {
		return nil, e
	}
	out := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if line == "" {
			continue
		}
		n, v, ok := strings.Cut(line, "\t")
		if !ok {
			return nil, fmt.Errorf("invalid refs output")
		}
		out[n] = v
	}
	return out, nil
}
func syncIn(ctx context.Context, c JobContext, j Job) (map[string]any, error) {
	if e := validateSource(j.Source); e != nil {
		return nil, e
	}
	if !repoRE.MatchString(j.Repository) {
		return nil, fmt.Errorf("invalid repository")
	}
	repo, e := SafePath(c.Repos, j.Repository+".git")
	if e != nil {
		return nil, e
	}
	if !regular(filepath.Join(repo, "HEAD")) {
		return nil, fmt.Errorf("target bare repository unavailable")
	}
	if e = os.MkdirAll(c.Work, 0700); e != nil {
		return nil, e
	}
	work, e := os.MkdirTemp(c.Work, "sync-"+j.ID+"-")
	if e != nil {
		return nil, e
	}
	defer os.RemoveAll(work)
	clone := filepath.Join(work, "source.git")
	run := func(args ...string) ([]byte, error) { return command(ctx, jobEnv(), nil, "git", args...) }
	// One private bare clone supplies both refs and LFS inventory. Do not publish
	// ls-remote OIDs from an earlier, unrelated observation of the remote.
	if _, e = run("clone", "--bare", j.Source, clone); e != nil {
		return nil, e
	}
	remote, e := readRefs(ctx, clone)
	if e != nil {
		return nil, e
	}
	head, he := run("--git-dir", clone, "symbolic-ref", "HEAD")
	if he != nil {
		head = nil
	}
	old, e := readRefs(ctx, repo)
	if e != nil {
		return nil, e
	}
	for n, v := range remote {
		if o, ok := old[n]; ok && o != v && !j.AllowRewrite {
			if strings.HasPrefix(n, "refs/tags/") {
				return nil, fmt.Errorf("remote tag changed")
			} // fetch old objects into private clone for ancestry check
			if _, e = run("--git-dir", clone, "fetch", "--no-tags", repo, o); e != nil {
				return nil, e
			}
			if _, e = run("--git-dir", clone, "merge-base", "--is-ancestor", o, v); e != nil {
				return nil, fmt.Errorf("non-fast-forward branch: %s", n)
			}
		}
	}
	for n := range old {
		if _, ok := remote[n]; !ok && !j.AllowDelete {
			return nil, fmt.Errorf("remote deleted ref: %s", n)
		}
	}
	type object struct {
		OID  string `json:"oid"`
		Size int64  `json:"size"`
	}
	objects := []object{}
	if boolDefault(j.LFS, true) {
		b, e := run("--git-dir", clone, "ls-tree", "--name-only", "HEAD", "--", ".lfsconfig")
		if e != nil {
			return nil, e
		}
		if len(bytesTrim(b)) > 0 {
			return nil, fmt.Errorf("remote .lfsconfig is not accepted")
		}
		if _, e = run("--git-dir", clone, "lfs", "fetch", "--all", "origin"); e != nil {
			return nil, e
		}
		b, e = run("--git-dir", clone, "lfs", "ls-files", "--all", "--json")
		if e != nil {
			return nil, e
		}
		var inv struct {
			Files []object `json:"files"`
		}
		if e = json.Unmarshal(b, &inv); e != nil {
			return nil, e
		}
		objects = inv.Files
	}
	release, e := exclusiveFence(ctx, jobWriteRoot(c), j.ID, false)
	if e != nil {
		return nil, e
	}
	defer release(true)
	seen := map[string]int64{}
	for _, obj := range objects {
		if !oidRE.MatchString(obj.OID) || obj.Size < 0 {
			return nil, fmt.Errorf("invalid remote LFS object")
		}
		if size, ok := seen[obj.OID]; ok {
			if size != obj.Size {
				return nil, fmt.Errorf("conflicting LFS sizes")
			}
			continue
		}
		seen[obj.OID] = obj.Size
		src := filepath.Join(clone, "lfs/objects", obj.OID[:2], obj.OID[2:4], obj.OID)
		dst, e := SafePath(c.LFS, j.Repository+".git/objects/"+obj.OID[:2]+"/"+obj.OID[2:4]+"/"+obj.OID)
		if e != nil {
			return nil, e
		}
		if h, e := digest(dst); e == nil && h == obj.OID {
			st, e := os.Stat(dst)
			if e == nil && st.Size() == obj.Size {
				continue
			}
		}
		if e = copyObject(ctx, src, dst, obj.OID, obj.Size); e != nil {
			return nil, e
		}
	}
	// Fetch only into staging refs. User-visible refs change in one transaction.
	if _, e = run("--git-dir", repo, "fetch", "--no-tags", "--force", clone, "+refs/heads/*:refs/cgit-import/heads/*", "+refs/tags/*:refs/cgit-import/tags/*"); e != nil {
		return nil, e
	}
	format, e := run("--git-dir", repo, "rev-parse", "--show-object-format")
	if e != nil {
		return nil, e
	}
	zero := strings.Repeat("0", 40)
	if strings.TrimSpace(string(format)) == "sha256" {
		zero = strings.Repeat("0", 64)
	}
	keys := map[string]bool{}
	for n := range old {
		keys[n] = true
	}
	for n := range remote {
		keys[n] = true
	}
	names := []string{}
	for n := range keys {
		names = append(names, n)
	}
	sort.Strings(names)
	lines := []string{"start"}
	for _, n := range names {
		v, o := remote[n], old[n]
		if v == "" {
			v = zero
		}
		if o == "" {
			o = zero
		}
		lines = append(lines, "update "+n+" "+v+" "+o)
	}
	lines = append(lines, "prepare", "commit")
	if _, e = command(ctx, jobEnv(), strings.NewReader(strings.Join(lines, "\n")+"\n"), "git", "--git-dir", repo, "update-ref", "--stdin"); e != nil {
		return nil, e
	}
	if ref := strings.TrimSpace(string(head)); remote[ref] != "" {
		if _, e = run("--git-dir", repo, "symbolic-ref", "HEAD", ref); e != nil {
			return nil, e
		}
	}
	return map[string]any{"refs_updated": len(names), "lfs_objects": len(seen)}, nil
}
func bytesTrim(b []byte) []byte { return []byte(strings.TrimSpace(string(b))) }
func copyObject(ctx context.Context, src, dst, oid string, size int64) error {
	if e := os.MkdirAll(filepath.Dir(dst), 0755); e != nil {
		return e
	}
	in, e := os.Open(src)
	if e != nil {
		return e
	}
	defer in.Close()
	f, e := os.CreateTemp(filepath.Dir(dst), ".upload-")
	if e != nil {
		return e
	}
	defer f.Close()
	defer os.Remove(f.Name())
	buf := make([]byte, 1<<20)
	var n int64
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		nr, er := in.Read(buf)
		if nr > 0 {
			nw, e := f.Write(buf[:nr])
			if e != nil {
				return e
			}
			n += int64(nw)
		}
		if er == io.EOF {
			break
		}
		if er != nil {
			return er
		}
	}
	if n != size {
		return fmt.Errorf("remote LFS size mismatch")
	}
	if e = f.Sync(); e != nil {
		return e
	}
	if e = f.Chmod(0644); e != nil {
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	h, e := digest(f.Name())
	if e != nil {
		return e
	}
	if h != oid {
		return fmt.Errorf("remote LFS hash mismatch")
	}
	if e = os.Rename(f.Name(), dst); e != nil {
		return e
	}
	return syncDir(filepath.Dir(dst))
}
func executeJob(ctx context.Context, c JobContext, j Job) (map[string]any, error) {
	switch j.Type {
	case "check":
		cfg, e := LoadConfigAt(c.RuntimeConfig)
		if e != nil {
			return nil, e
		}
		r := Check(ctx, cfg, boolDefault(j.Git, true), boolDefault(j.LFS, true))
		if !r.OK {
			return nil, fmt.Errorf("integrity check failed: %s", strings.Join(r.Findings, "; "))
		}
		return map[string]any{"repositories": len(r.Repositories)}, nil
	case "archive":
		return archive(ctx, c, j)
	case "backup":
		return backup(ctx, c, j)
	case "sync-in":
		return syncIn(ctx, c, j)
	case "maintenance":
		if !filepath.IsAbs(j.Script) || symlink(j.Script) || !regular(j.Script) {
			return nil, fmt.Errorf("maintenance script unavailable")
		}
		release, e := exclusiveFence(ctx, jobWriteRoot(c), j.ID, false)
		if e != nil {
			return nil, e
		}
		defer release(true)
		env := jobEnv()
		env = append(env, "CGIT_REPOS="+c.Repos, "CGIT_LFS="+c.LFS, "CGIT_ACCESS="+c.Access)
		_, e = command(ctx, env, nil, j.Script, j.Args...)
		return map[string]any{"script": filepath.Base(j.Script)}, e
	}
	return nil, fmt.Errorf("unsupported job")
}
func LoadConfigAt(p string) (*Config, error) {
	var c Config
	if e := readJSON(p, &c); e != nil {
		return nil, e
	}
	return &c, nil
}
func runJob(parent context.Context, c JobContext, j Job) (map[string]any, error) {
	if e := os.MkdirAll(c.State, 0700); e != nil {
		return nil, e
	}
	lockpath := filepath.Join(c.State, "runner.lock")
	if symlink(lockpath) {
		return nil, fmt.Errorf("unsafe runner lock")
	}
	lock, e := os.OpenFile(lockpath, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	defer lock.Close()
	if e = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		return nil, fmt.Errorf("another maintenance job is already running")
	}
	p := filepath.Join(c.State, j.ID+".json")
	var previous map[string]any
	readJSON(p, &previous)
	started := time.Now()
	state := map[string]any{"schema_version": 1, "job_id": j.ID, "run_id": started.UTC().Format("20060102T150405Z") + "-" + strconv.Itoa(os.Getpid()), "status": "running", "started": now(), "last_success": previous["last_success"]}
	if e = atomicWrite(p, jsonBytes(state), 0600); e != nil {
		return nil, e
	}
	timeout := j.Timeout
	if timeout == 0 {
		timeout = 3600
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(timeout)*time.Second)
	defer cancel()
	summary, runErr := executeJob(ctx, c, j)
	state["finished"], state["seconds"] = now(), time.Since(started).Seconds()
	if runErr != nil {
		state["status"], state["error"] = "failed", runErr.Error()
	} else {
		state["status"], state["summary"], state["last_success"] = "success", summary, now()
	}
	if e = atomicWrite(p, jsonBytes(state), 0600); e != nil {
		return nil, e
	}
	return state, runErr
}
func verifyRestored(ctx context.Context, destination string) (int, error) {
	repos, lfs, access := filepath.Join(destination, "repos"), filepath.Join(destination, "lfs"), filepath.Join(destination, "access")
	for _, p := range []string{repos, lfs, access} {
		if symlink(p) || !isDir(p) {
			return 0, fmt.Errorf("restored directory missing or unsafe")
		}
	}
	s, e := loadSnapshot(access)
	if e != nil {
		return 0, e
	}
	if e = checkDigest(filepath.Join(access, ".gitolite/conf/gitolite.conf-compiled.pm"), s.CommonDigest); e != nil {
		return 0, e
	}
	for name, h := range s.Repositories {
		p, e := SafePath(repos, name+".git")
		if e != nil {
			return 0, e
		}
		if e = checkDigest(filepath.Join(p, "gl-conf"), h); e != nil {
			return 0, e
		}
	}
	if !regular(filepath.Join(access, "ssh/authorized_keys")) || symlink(filepath.Join(access, "ssh/authorized_keys")) {
		return 0, fmt.Errorf("restored SSH keys unavailable")
	}
	key, e := os.ReadFile(filepath.Join(access, "lfs-token.key"))
	if e != nil || len(key) != 32 || symlink(filepath.Join(access, "lfs-token.key")) {
		return 0, fmt.Errorf("restored LFS token key invalid")
	}
	r := Check(ctx, &Config{Repos: repos, LFS: lfs, AuthMode: "public"}, true, true)
	if !r.OK || len(r.Repositories) != len(s.Repositories) {
		return 0, fmt.Errorf("restored Git/LFS inventory check failed: %s", strings.Join(r.Findings, "; "))
	}
	return len(s.Repositories), nil
}
func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }
func Jobs(args []string) error {
	parent, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	f := flag.NewFlagSet("jobs", flag.ContinueOnError)
	c := JobContext{}
	f.StringVar(&c.Config, "config", "/jobs/jobs.json", "")
	f.StringVar(&c.State, "state", "/job-state", "")
	f.StringVar(&c.Work, "work", "/job-work", "")
	f.StringVar(&c.Repos, "repos", "/repos", "")
	f.StringVar(&c.LFS, "lfs", "/lfs", "")
	f.StringVar(&c.Access, "access", "/access", "")
	f.StringVar(&c.Backups, "backups", "/backups", "")
	f.StringVar(&c.RuntimeConfig, "runtime-config", configPath(), "")
	if e := f.Parse(args); e != nil {
		return e
	}
	rest := f.Args()
	if len(rest) == 0 {
		return fmt.Errorf("jobs subcommand required")
	}
	sub := rest[0]
	sf := flag.NewFlagSet(sub, flag.ContinueOnError)
	id := sf.String("id", "", "")
	output := sf.String("output", "", "")
	repository := sf.String("repository", "", "")
	password := sf.String("password-file", "", "")
	snap := sf.String("snapshot", "latest", "")
	destination := sf.String("destination", "", "")
	timeout := sf.Int("timeout", 86400, "")
	if e := sf.Parse(rest[1:]); e != nil {
		return e
	}
	if sub == "restore" {
		if *destination == "" || symlink(*destination) {
			return fmt.Errorf("safe empty destination required")
		}
		dst, e := filepath.Abs(*destination)
		if e != nil || dst == "/" {
			return fmt.Errorf("unsafe restore destination")
		}
		entries, e := os.ReadDir(dst)
		if e != nil && !os.IsNotExist(e) {
			return e
		}
		if len(entries) > 0 {
			return fmt.Errorf("restore destination must be absent or empty")
		}
		if e = os.MkdirAll(dst, 0700); e != nil {
			return e
		}
		base, e := resticArgs(*repository, *password)
		if e != nil {
			return e
		}
		ctx, cancel := context.WithTimeout(parent, time.Duration(*timeout)*time.Second)
		defer cancel()
		if _, e = command(ctx, resticEnv(), nil, "restic", append(base, "restore", *snap, "--target", dst, "--verify")...); e != nil {
			return e
		}
		n, e := verifyRestored(ctx, dst)
		if e != nil {
			return e
		}
		printJSON(map[string]any{"destination": dst, "snapshot": *snap, "verified": map[string]int{"repositories": n}})
		return nil
	}
	cfg, e := loadJobs(c.Config)
	if e != nil {
		return e
	}
	switch sub {
	case "validate":
		printJSON(map[string]any{"ok": true, "jobs": len(cfg.Jobs)})
	case "status":
		states := map[string]any{}
		for _, j := range cfg.Jobs {
			var state any
			p := filepath.Join(c.State, j.ID+".json")
			if exists(p) {
				if e = readJSON(p, &state); e != nil {
					return e
				}
			}
			states[j.ID] = state
		}
		printJSON(map[string]any{"ok": true, "jobs": states})
	case "run":
		for _, j := range cfg.Jobs {
			if j.ID == *id {
				state, e := runJob(parent, c, j)
				if state != nil {
					printJSON(state)
				}
				return e
			}
		}
		return fmt.Errorf("unknown job ID")
	case "crontab":
		bin, e := os.Executable()
		if e != nil {
			return e
		}
		lines := []string{"TZ=" + cfg.Timezone}
		for _, j := range cfg.Jobs {
			if !j.Enabled {
				continue
			}
			cmd := []string{bin, "jobs", "--config", c.Config, "--state", c.State, "--work", c.Work, "--repos", c.Repos, "--lfs", c.LFS, "--access", c.Access, "--backups", c.Backups, "--runtime-config", c.RuntimeConfig, "run", "--id", j.ID}
			for i := range cmd {
				cmd[i] = shellQuote(cmd[i])
			}
			lines = append(lines, j.Cron+" "+strings.Join(cmd, " "))
		}
		if e = atomicWrite(*output, []byte(strings.Join(lines, "\n")+"\n"), 0600); e != nil {
			return e
		}
		printJSON(map[string]any{"ok": true, "scheduled": len(lines) - 1, "output": *output})
	default:
		return fmt.Errorf("unknown jobs command")
	}
	return nil
}

func jobWriteRoot(c JobContext) string {
	cfg, err := LoadConfigAt(c.RuntimeConfig)
	if err == nil && (cfg.CgitConfig != "" || cfg.WriteRoot != "" || cfg.Access != nil) {
		return writeRoot(cfg)
	}
	return c.Access
}

func resticEnv() []string {
	env := jobEnv()
	for _, k := range []string{"RESTIC_CACHE_DIR", "RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION", "AWS_REGION", "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "B2_ACCOUNT_ID", "B2_ACCOUNT_KEY", "AZURE_ACCOUNT_NAME", "AZURE_ACCOUNT_KEY", "GOOGLE_APPLICATION_CREDENTIALS"} {
		if v := os.Getenv(k); v != "" {
			env = setEnv(env, k, v)
		}
	}
	return env
}
