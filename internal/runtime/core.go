// Package runtime implements the cgit service boundary. Git and Gitolite remain
// the authoritative implementations of repository transport and ACL semantics.
package runtime

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
)

type Error struct {
	Status  int
	Message string
}

func (e *Error) Error() string              { return e.Message }
func fail(status int, message string) error { return &Error{status, message} }

var oidRE = regexp.MustCompile(`^[a-f0-9]{64}$`)
var repoRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._@+\-]*(/[A-Za-z0-9][A-Za-z0-9._@+\-]*)*$`)
var userRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._@+\-]{0,127}$`)
var pointerRE = regexp.MustCompile(`^version https://git-lfs.github.com/spec/v1\r?\noid sha256:([a-f0-9]{64})\r?\nsize (0|[1-9][0-9]*)\r?\n?$`)

type Policy struct {
	ProjectList  string `json:"project_list,omitempty"`
	StrictExport string `json:"strict_export,omitempty"`
}
type AccessOptions struct {
	Root     string `json:"root"`
	Gitolite string `json:"gitolite"`
}
type Config struct {
	MaxRequestSize   int64          `json:"max_request_size,omitempty"`
	LFSMinFreeBytes  int64          `json:"lfs_min_free_bytes,omitempty"`
	PackThreads      int            `json:"pack_threads,omitempty"`
	PackWindowMemory int64          `json:"pack_window_memory,omitempty"`
	WriteRoot        string         `json:"write_root,omitempty"`
	Repos            string         `json:"repos"`
	LFS              string         `json:"lfs"`
	BaseURL          string         `json:"base_url"`
	AuthMode         string         `json:"auth_mode"`
	Username         string         `json:"username"`
	PasswordFile     string         `json:"password_file"`
	HTTPPush         bool           `json:"http_push"`
	SSHPush          bool           `json:"ssh_push"`
	MaxSize          int64          `json:"max_size"`
	Cgit             string         `json:"cgit"`
	CgitConfig       string         `json:"cgit_config"`
	Policy           Policy         `json:"repository_policy"`
	Access           *AccessOptions `json:"access,omitempty"`
}

func envDefault(k, v string) string {
	if s := os.Getenv(k); s != "" {
		return s
	}
	return v
}
func configPath() string {
	return envDefault("CGIT_RUNTIME_CONFIG", "/var/cache/cgit/runtime/settings.json")
}
func LoadConfig() (*Config, error) {
	var c Config
	if err := readJSON(configPath(), &c); err != nil {
		return nil, err
	}
	if c.Repos == "" || c.LFS == "" || !contains([]string{"public", "private", "access"}, c.AuthMode) {
		return nil, fmt.Errorf("invalid runtime configuration")
	}
	return &c, nil
}
func contains(a []string, s string) bool {
	for _, v := range a {
		if s == v {
			return true
		}
	}
	return false
}
func exists(p string) bool  { _, e := os.Stat(p); return e == nil }
func isDir(p string) bool   { s, e := os.Stat(p); return e == nil && s.IsDir() }
func regular(p string) bool { s, e := os.Stat(p); return e == nil && s.Mode().IsRegular() }
func symlink(p string) bool { s, e := os.Lstat(p); return e == nil && s.Mode()&os.ModeSymlink != 0 }
func executable(p string) bool {
	s, e := os.Stat(p)
	return e == nil && s.Mode().IsRegular() && s.Mode()&0111 != 0
}
func readJSON(p string, target any) error {
	f, e := os.Open(p)
	if e != nil {
		return e
	}
	defer f.Close()
	b, e := io.ReadAll(io.LimitReader(f, 1024*1024+1))
	if e != nil {
		return e
	}
	if len(b) > 1024*1024 {
		return fmt.Errorf("configuration exceeds size limit")
	}
	return json.Unmarshal(b, target)
}
func jsonBytes(v any) []byte {
	b, e := json.Marshal(v)
	if e != nil {
		panic(e)
	}
	return b
}
func printJSON(v any) { fmt.Println(string(jsonBytes(v))) }
func digest(p string) (string, error) {
	f, e := os.Open(p)
	if e != nil {
		return "", e
	}
	defer f.Close()
	h := sha256.New()
	if _, e = io.Copy(h, f); e != nil {
		return "", e
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
func hash(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func atomicWrite(p string, b []byte, mode os.FileMode) error {
	if e := os.MkdirAll(filepath.Dir(p), 0700); e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(p), ".publish-")
	if e != nil {
		return e
	}
	defer os.Remove(f.Name())
	if e = f.Chmod(mode); e == nil {
		_, e = f.Write(b)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e != nil {
		return e
	}
	if e = os.Rename(f.Name(), p); e != nil {
		return e
	}
	return syncDir(filepath.Dir(p))
}
func syncDir(p string) error {
	f, e := os.Open(p)
	if e != nil {
		return e
	}
	defer f.Close()
	return f.Sync()
}
func SafePath(root, relative string) (string, error) {
	if relative == "" || strings.ContainsAny(relative, "\\\x00") {
		return "", fail(404, "Not found")
	}
	for _, c := range relative {
		if c < 32 || c == 127 {
			return "", fail(404, "Not found")
		}
	}
	base, e := filepath.Abs(root)
	if e != nil {
		return "", e
	}
	base, e = filepath.EvalSymlinks(base)
	if e != nil {
		return "", e
	}
	p := base
	for _, part := range strings.Split(relative, "/") {
		if part == "" || strings.HasPrefix(part, ".") {
			return "", fail(404, "Not found")
		}
		p = filepath.Join(p, part)
		s, e := os.Lstat(p)
		if e != nil && !os.IsNotExist(e) {
			return "", e
		}
		if e == nil && s.Mode()&os.ModeSymlink != 0 {
			return "", fail(404, "Not found")
		}
	}
	return p, nil
}
func ObjectPath(c *Config, name, oid string) (string, error) {
	if !oidRE.MatchString(oid) {
		return "", fail(422, "Invalid SHA-256 object ID")
	}
	return SafePath(c.LFS, name+"/objects/"+oid[:2]+"/"+oid[2:4]+"/"+oid)
}
func cleanEnv(home string) []string {
	env := []string{"PATH=" + envDefault("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG=C", "TZ=" + envDefault("TZ", "UTC"), "GIT_CONFIG_GLOBAL=" + os.DevNull, "GIT_CONFIG_NOSYSTEM=1", "GIT_TERMINAL_PROMPT=0"}
	if home != "" {
		env = append(env, "HOME="+home)
	}
	return env
}
func setEnv(env []string, k, v string) []string {
	out := make([]string, 0, len(env)+1)
	for _, s := range env {
		if !strings.HasPrefix(s, k+"=") {
			out = append(out, s)
		}
	}
	return append(out, k+"="+v)
}
func command(ctx context.Context, env []string, input io.Reader, name string, args ...string) ([]byte, error) {
	resolved := name
	if !strings.ContainsRune(name, '/') {
		for _, entry := range env {
			if strings.HasPrefix(entry, "PATH=") {
				for _, dir := range filepath.SplitList(strings.TrimPrefix(entry, "PATH=")) {
					candidate := filepath.Join(dir, name)
					if executable(candidate) {
						resolved = candidate
						break
					}
				}
				break
			}
		}
	}
	cmd := exec.CommandContext(ctx, resolved, args...)
	cmd.Env = env
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return os.ErrProcessDone
		}
		err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if err == syscall.ESRCH {
			return os.ErrProcessDone
		}
		return err
	}
	cmd.WaitDelay = 2 * time.Second
	cmd.Stdin = input
	var out, errout bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errout
	err := cmd.Run()
	if err != nil {
		return out.Bytes(), fmt.Errorf("%s failed: %w: %s", filepath.Base(name), err, strings.TrimSpace(errout.String()))
	}
	return out.Bytes(), nil
}
func boundedCommand(env []string, input io.Reader, name string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	return command(ctx, env, input, name, args...)
}
func git(repo string, args ...string) ([]byte, error) {
	a := append([]string{"-c", "safe.directory=" + repo, "--git-dir", repo}, args...)
	return boundedCommand(cleanEnv(""), nil, "git", a...)
}
func exitCode(err error) int {
	var e *exec.ExitError
	if errors.As(err, &e) {
		return e.ExitCode()
	}
	return -1
}
func settings(source string) ([][2]string, error) {
	if strings.ContainsRune(source, 0) {
		return nil, fmt.Errorf("NUL in configuration")
	}
	out := [][2]string{}
	for _, raw := range strings.Split(strings.ReplaceAll(source, "\r\n", "\n"), "\n") {
		s := strings.TrimLeft(raw, " \t\r\v\f")
		if s == "" || strings.HasPrefix(s, "#") || strings.HasPrefix(s, ";") {
			continue
		}
		k, v, ok := strings.Cut(s, "=")
		if !ok {
			return nil, fmt.Errorf("malformed cgit configuration")
		}
		out = append(out, [2]string{k, v})
	}
	return out, nil
}
func projectEntries(p string) ([]string, error) {
	b, e := os.ReadFile(p)
	if e != nil {
		return nil, e
	}
	out := []string{}
	for _, s := range strings.Split(strings.TrimSuffix(string(b), "\n"), "\n") {
		if s == "" {
			continue
		}
		for _, part := range strings.Split(s, "/") {
			if part == "" || strings.HasPrefix(part, ".") || strings.ContainsAny(part, "\\\r\t\x00") {
				return nil, fmt.Errorf("noncanonical project-list entry")
			}
		}
		out = append(out, s)
	}
	return out, nil
}
func ignored(repo string) (bool, error) {
	b, e := git(repo, "config", "--get", "cgit.ignore")
	value := "0"
	if e == nil {
		value = strings.TrimSpace(string(b))
	} else if exitCode(e) != 1 {
		return false, e
	}
	if exists(filepath.Join(repo, "cgitrc")) {
		b, e = os.ReadFile(filepath.Join(repo, "cgitrc"))
		if e != nil {
			return false, e
		}
		ss, e := settings(string(b))
		if e != nil {
			return false, e
		}
		for _, kv := range ss {
			if kv[0] == "ignore" {
				value = kv[1]
			}
		}
	}
	if value != "0" && value != "1" {
		return false, fmt.Errorf("cgit.ignore must be 0 or 1")
	}
	return value == "1", nil
}
func scan(root string) ([]string, error) {
	out := []string{}
	err := filepath.WalkDir(root, func(p string, d os.DirEntry, e error) error {
		if e != nil {
			return e
		}
		if p == root {
			return nil
		}
		if strings.HasPrefix(d.Name(), ".") {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if d.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("symlink paths are not supported: %s", p)
		}
		if d.IsDir() {
			if regular(filepath.Join(p, "HEAD")) && isDir(filepath.Join(p, "objects")) {
				if _, e := ignored(p); e != nil {
					return e
				}
				rel, e := filepath.Rel(root, p)
				if e != nil {
					return e
				}
				out = append(out, filepath.ToSlash(rel))
				return filepath.SkipDir
			}
			if exists(filepath.Join(p, ".git")) {
				return fmt.Errorf("only bare repositories supported")
			}
		}
		return nil
	})
	return out, err
}
func repository(c *Config, name string, serving bool) (string, string, error) {
	candidates := []string{name + ".git", name}
	if strings.HasSuffix(name, ".git") {
		candidates = []string{name}
	}
	for _, n := range candidates {
		p, e := SafePath(c.Repos, n)
		if e != nil {
			return "", "", e
		}
		if !regular(filepath.Join(p, "HEAD")) || !isDir(filepath.Join(p, "objects")) {
			continue
		}
		if serving {
			if c.Policy.ProjectList != "" {
				entries, e := projectEntries(c.Policy.ProjectList)
				if e != nil {
					return "", "", e
				}
				ok := false
				for _, s := range entries {
					if n == s || strings.HasPrefix(n, s+"/") {
						ok = true
					}
				}
				if !ok {
					return "", "", fail(404, "Not found")
				}
			}
			ig, e := ignored(p)
			if e != nil {
				return "", "", e
			}
			if ig || exists(filepath.Join(p, "noweb")) || (c.Policy.StrictExport != "" && !exists(filepath.Join(p, c.Policy.StrictExport))) {
				return "", "", fail(404, "Not found")
			}
		}
		return p, n, nil
	}
	return "", "", fail(404, "Repository not found")
}

func writeRoot(c *Config) string {
	if c.Access != nil {
		return c.Access.Root
	}
	if c.WriteRoot != "" {
		return c.WriteRoot
	}
	return filepath.Join(filepath.Dir(filepath.Dir(c.CgitConfig)), "coordination")
}
