package runtime

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func write(t *testing.T, p, s string) {
	t.Helper()
	if e := os.MkdirAll(filepath.Dir(p), 0700); e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(p, []byte(s), 0600); e != nil {
		t.Fatal(e)
	}
}
func testGit(t *testing.T, args ...string) []byte {
	t.Helper()
	env := append(cleanEnv(""), "GIT_AUTHOR_NAME=Fixture", "GIT_AUTHOR_EMAIL=fixture@localhost", "GIT_COMMITTER_NAME=Fixture", "GIT_COMMITTER_EMAIL=fixture@localhost")
	b, e := boundedCommand(env, nil, "git", args...)
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func fixture(t *testing.T) (JobContext, string) {
	t.Helper()
	root := t.TempDir()
	c := JobContext{Repos: filepath.Join(root, "repos"), LFS: filepath.Join(root, "lfs"), Access: filepath.Join(root, "access"), State: filepath.Join(root, "state"), Work: filepath.Join(root, "job-work"), Backups: filepath.Join(root, "backups")}
	for _, p := range []string{c.Repos, c.LFS, c.Access, c.Backups} {
		if e := os.MkdirAll(p, 0700); e != nil {
			t.Fatal(e)
		}
	}
	repo := filepath.Join(c.Repos, "demo.git")
	testGit(t, "init", "--bare", "-b", "main", repo)
	work := filepath.Join(root, "work")
	testGit(t, "init", "-b", "main", work)
	write(t, filepath.Join(work, "README.md"), "# fixture\n")
	testGit(t, "-C", work, "add", ".")
	testGit(t, "-C", work, "commit", "-m", "initial")
	testGit(t, "-C", work, "push", repo, "HEAD")
	return c, work
}
func TestSafePaths(t *testing.T) {
	root := t.TempDir()
	os.Mkdir(filepath.Join(root, "safe"), 0700)
	os.Symlink(t.TempDir(), filepath.Join(root, "link"))
	for _, name := range []string{"", "../a", "a/../b", ".hidden", "a//b", "a/", "/absolute", "a\\b", "a\x00b", "a\nb", "link/repo.git"} {
		t.Run(name, func(t *testing.T) {
			if _, e := SafePath(root, name); e == nil {
				t.Fatal("unsafe path accepted")
			}
		})
	}
	if _, e := SafePath(root, "safe/group/demo.git"); e != nil {
		t.Fatal(e)
	}
}
func TestPointers(t *testing.T) {
	oid := strings.Repeat("a", 64)
	for _, size := range []string{"0", "123"} {
		b := []byte("version https://git-lfs.github.com/spec/v1\noid sha256:" + oid + "\nsize " + size + "\n")
		if got, _, ok := parsePointer(b); !ok || got != oid {
			t.Fatal("valid pointer rejected")
		}
	}
	for _, size := range []string{"-1", "01", "true", "9223372036854775808"} {
		_, _, ok := parsePointer([]byte("version https://git-lfs.github.com/spec/v1\noid sha256:" + oid + "\nsize " + size + "\n"))
		if ok {
			t.Fatal("invalid pointer size accepted")
		}
	}
}
func TestCanonicalPythonSnapshotVector(t *testing.T) {
	v := map[string]any{"z": "中文<&>", "a": true}
	if got := string(canonicalObject(v)); got != `{"a":true,"z":"\u4e2d\u6587<&>"}` {
		t.Fatal(got)
	}
}
func TestCredentialHash(t *testing.T) {
	secret := []byte("synthetic-secret-123456")
	h, e := hashSecret(secret)
	if e != nil {
		t.Fatal(e)
	}
	if !verifySecret(secret, h) || verifySecret([]byte("wrong"), h) {
		t.Fatal("credential verification")
	}
	if _, e = hashSecret([]byte("short")); e == nil {
		t.Fatal("short secret accepted")
	}
	if verifySecret(bytes.Repeat([]byte("x"), 1025), h) {
		t.Fatal("oversize accepted")
	}
}
func TestTokenScopesAndKeyLifecycle(t *testing.T) {
	root := t.TempDir()
	c := &Config{Access: &AccessOptions{Root: root}}
	if _, e := tokenSecret(root); e == nil {
		t.Fatal("key created outside maintenance")
	}
	write(t, filepath.Join(root, "maintenance"), "test")
	key, e := tokenSecret(root)
	if e != nil || len(key) != 32 {
		t.Fatal(e)
	}
	st, _ := os.Stat(filepath.Join(root, "lfs-token.key"))
	if st.Mode().Perm() != 0600 {
		t.Fatal("key permissions")
	}
	generation := strings.Repeat("a", 64)
	token, e := issueToken(c, "owner", "group/demo", "download", generation)
	if e != nil {
		t.Fatal(e)
	}
	if u, e := verifyToken(c, token, "group/demo", "download", generation); e != nil || u != "owner" {
		t.Fatal(u, e)
	}
	for _, tc := range []struct{ token, repo, op, gen string }{{token, "other", "download", generation}, {token, "group/demo", "upload", generation}, {token, "group/demo", "download", strings.Repeat("b", 64)}, {token + "x", "group/demo", "download", generation}, {"invalid", "group/demo", "download", generation}} {
		if _, e := verifyToken(c, tc.token, tc.repo, tc.op, tc.gen); e == nil {
			t.Fatal("invalid token accepted")
		}
	}
	os.Remove(filepath.Join(root, "maintenance"))
	os.Remove(filepath.Join(root, "lfs-token.key"))
	if _, e := verifyToken(c, token, "group/demo", "download", generation); e == nil {
		t.Fatal("missing key accepted")
	}
}
func TestWriteFenceWaitsAndFailurePolicy(t *testing.T) {
	root := t.TempDir()
	held, e := holdShared(root)
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	if _, e = exclusiveFence(ctx, root, "backup", false); e == nil {
		t.Fatal("backup did not wait for writer")
	}
	if exists(filepath.Join(root, "maintenance")) {
		t.Fatal("timeout marker not cleaned")
	}
	held.Close()
	release, e := exclusiveFence(context.Background(), root, "apply", true)
	if e != nil {
		t.Fatal(e)
	}
	release(false)
	if !exists(filepath.Join(root, "maintenance")) {
		t.Fatal("failed apply opened requests")
	}
	os.Remove(filepath.Join(root, "maintenance"))
	release, e = exclusiveFence(context.Background(), root, "apply", true)
	if e != nil {
		t.Fatal(e)
	}
	release(true)
	if exists(filepath.Join(root, "maintenance")) {
		t.Fatal("success marker retained")
	}
}
func TestSyncRealLFSAndCorruptRepair(t *testing.T) {
	c, work := fixture(t)
	payload := []byte("real local LFS bytes")
	oid := hash(payload)
	write(t, filepath.Join(work, "asset.bin"), "version https://git-lfs.github.com/spec/v1\noid sha256:"+oid+"\nsize 20\n")
	testGit(t, "-C", work, "add", ".")
	testGit(t, "-C", work, "commit", "-m", "pointer")
	source := filepath.Join(t.TempDir(), "source.git")
	testGit(t, "clone", "--bare", work, source)
	src := filepath.Join(source, "lfs/objects", oid[:2], oid[2:4], oid)
	write(t, src, string(payload))
	dst := filepath.Join(c.LFS, "demo.git/objects", oid[:2], oid[2:4], oid)
	write(t, dst, strings.Repeat("x", len(payload)))
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	result, e := syncIn(ctx, c, Job{ID: "sync", Source: source, Repository: "demo"})
	if e != nil {
		t.Fatal(e)
	}
	if result["lfs_objects"] != 1 {
		t.Fatal(result)
	}
	b, e := os.ReadFile(dst)
	if e != nil || !bytes.Equal(b, payload) {
		t.Fatal("corrupt LFS not repaired", e)
	}
	r := Check(ctx, &Config{Repos: c.Repos, LFS: c.LFS, AuthMode: "public"}, true, true)
	if !r.OK {
		t.Fatal(r.Findings)
	}
	os.Remove(dst)
	if Check(ctx, &Config{Repos: c.Repos, LFS: c.LFS, AuthMode: "public"}, true, true).OK {
		t.Fatal("missing LFS not detected")
	}
}
func TestSyncRejectsConfigSymlinkAndRewrite(t *testing.T) {
	c, work := fixture(t)
	source := filepath.Join(t.TempDir(), "source.git")
	testGit(t, "clone", "--bare", work, source)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	os.Symlink(c.Repos, filepath.Join(c.Repos, "group"))
	if _, e := syncIn(ctx, c, Job{ID: "sync", Source: source, Repository: "group/demo"}); e == nil {
		t.Fatal("symlink parent accepted")
	}
	os.Remove(filepath.Join(c.Repos, "group"))
	write(t, filepath.Join(work, ".lfsconfig"), "[lfs]\nurl=http://127.0.0.1:1/untrusted\n")
	testGit(t, "-C", work, "add", ".")
	testGit(t, "-C", work, "commit", "-m", "config")
	testGit(t, "-C", work, "push", source, "HEAD")
	old := testGit(t, "--git-dir", filepath.Join(c.Repos, "demo.git"), "rev-parse", "HEAD")
	if _, e := syncIn(ctx, c, Job{ID: "sync", Source: source, Repository: "demo"}); e == nil || !strings.Contains(e.Error(), "lfsconfig") {
		t.Fatal(e)
	}
	if got := testGit(t, "--git-dir", filepath.Join(c.Repos, "demo.git"), "rev-parse", "HEAD"); !bytes.Equal(got, old) {
		t.Fatal("refs changed on rejection")
	}
}
func TestSyncRejectsHelpers(t *testing.T) {
	for _, s := range []string{"ext::sh -c touch /tmp/no", "file:///tmp/repo.git", "relative/repo", "git@host:repo.git", "https://token@host/repo.git", "https://host/repo.git?token=secret", "ssh://user:secret@host/repo.git"} {
		if e := validateSource(s); e == nil {
			t.Fatal("unsafe source accepted", s)
		}
	}
}
func TestObjectCopyFailureLeavesDestination(t *testing.T) {
	root := t.TempDir()
	src, dst := filepath.Join(root, "src"), filepath.Join(root, "objects", "dst")
	write(t, src, "bad")
	write(t, dst, "original")
	if e := copyObject(context.Background(), src, dst, hash([]byte("expected")), 3); e == nil {
		t.Fatal("hash mismatch accepted")
	}
	b, _ := os.ReadFile(dst)
	if string(b) != "original" {
		t.Fatal("existing object damaged")
	}
	matches, _ := filepath.Glob(filepath.Join(root, "objects/.upload-*"))
	if len(matches) != 0 {
		t.Fatal("temporary upload leaked")
	}
}
func TestGatewayProcess(t *testing.T) {
	if os.Getenv("CGIT_GO_TEST_GATEWAY") == "1" {
		Gateway()
		os.Exit(0)
	}
}
func gatewayRequest(t *testing.T, c *Config, env map[string]string, body string) []byte {
	t.Helper()
	root := t.TempDir()
	cfg := filepath.Join(root, "settings.json")
	write(t, cfg, string(jsonBytes(c)))
	cmd := exec.Command(os.Args[0], "-test.run=^TestGatewayProcess$")
	cmd.Env = append(cleanEnv(""), "CGIT_GO_TEST_GATEWAY=1", "CGIT_RUNTIME_CONFIG="+cfg, "REQUEST_METHOD=GET", "REQUEST_URI=/")
	for k, v := range env {
		cmd.Env = setEnv(cmd.Env, k, v)
	}
	cmd.Stdin = strings.NewReader(body)
	out, e := cmd.Output()
	if e != nil {
		t.Fatal(e)
	}
	return out
}
func TestGatewayPrivateAndLogin(t *testing.T) {
	c := &Config{Repos: t.TempDir(), LFS: t.TempDir(), AuthMode: "private"}
	out := gatewayRequest(t, c, nil, "")
	if !bytes.Contains(out, []byte("Status: 401")) || !bytes.Contains(out, []byte("WWW-Authenticate: Basic")) {
		t.Fatal(string(out))
	}
	c.AuthMode = "public"
	out = gatewayRequest(t, c, map[string]string{"REQUEST_URI": "/_login"}, "")
	if !bytes.Contains(out, []byte("Status: 404")) {
		t.Fatal(string(out))
	}
}
func TestObjectInfoRejectsNonIntegerSize(t *testing.T) {
	for _, size := range []string{"true", "null", "-1", "1.0", "\"1\""} {
		_, _, e := objectInfo([]byte(`{"oid":"`+strings.Repeat("a", 64)+`","size":`+size+`}`), 1024)
		if e == nil {
			t.Fatal("invalid size", size)
		}
	}
}
func TestConfigureDefaultsAndRejectsBadFlags(t *testing.T) {
	root := t.TempDir()
	repos := filepath.Join(root, "repos")
	os.Mkdir(repos, 0700)
	source := filepath.Join(root, "source")
	write(t, source, "scan-path="+repos+"\n")
	for _, k := range []string{"CGIT_AUTH_MODE", "CGIT_USERNAME", "CGIT_PASSWORD_FILE", "CGIT_ACCESS_ROOT", "CGIT_GITOLITE", "CGIT_HTTP_PUSH", "CGIT_SSH_PUSH", "CGIT_BASE_URL", "CGIT_SSH_CLONE_URL", "CGIT_LFS_MAX_SIZE"} {
		t.Setenv(k, "")
	}
	t.Setenv("CGIT_RUNTIME_DIR", filepath.Join(root, "runtime"))
	args := []string{"--source", source, "--repos", repos}
	if e := Configure(args); e != nil {
		t.Fatal(e)
	}
	var c Config
	if e := readJSON(filepath.Join(root, "runtime/settings.json"), &c); e != nil {
		t.Fatal(e)
	}
	if c.HTTPPush || c.AuthMode != "public" || c.Access != nil {
		t.Fatal(c)
	}
	for _, kv := range [][2]string{{"CGIT_HTTP_PUSH", "true"}, {"CGIT_AUTH_MODE", "unknown"}, {"CGIT_BASE_URL", "https://example.test/subpath"}, {"CGIT_LFS_MAX_SIZE", "-1"}} {
		t.Run(kv[0], func(t *testing.T) {
			t.Setenv(kv[0], kv[1])
			if e := Configure(args); e == nil {
				t.Fatal("bad config accepted")
			}
		})
	}
}
func TestRenderFilterEscapesActiveHTML(t *testing.T) {
	r, w, e := os.Pipe()
	if e != nil {
		t.Fatal(e)
	}
	w.WriteString("# title\n<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n")
	w.Close()
	oldin, oldout := os.Stdin, os.Stdout
	os.Stdin = r
	out, ow, _ := os.Pipe()
	os.Stdout = ow
	defer func() { os.Stdin, os.Stdout = oldin, oldout }()
	if e = AboutFilter(nil); e != nil {
		t.Fatal(e)
	}
	ow.Close()
	b, _ := io.ReadAll(out)
	r.Close()
	out.Close()
	if bytes.Contains(b, []byte("<script")) || bytes.Contains(b, []byte("onerror")) {
		t.Fatal(string(b))
	}
}

func TestTokenExpirationAndFutureIssued(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "maintenance"), "test")
	key, e := tokenSecret(root)
	if e != nil {
		t.Fatal(e)
	}
	c := &Config{Access: &AccessOptions{Root: root}}
	now := time.Now().Unix()
	for _, times := range [][2]int64{{now - 400, now - 1}, {now + 120, now + 420}, {now, now + 7200}} {
		p := tokenPayload{"git-lfs", times[1], strings.Repeat("a", 64), times[0], "download", "demo", "reader"}
		raw := canonicalObject(p)
		h := hmac.New(sha256.New, key)
		h.Write(append([]byte("v1."), raw...))
		token := "v1." + base64.RawURLEncoding.EncodeToString(raw) + "." + base64.RawURLEncoding.EncodeToString(h.Sum(nil))
		if _, e = verifyToken(c, token, "demo", "download", strings.Repeat("a", 64)); e == nil {
			t.Fatal("invalid lifetime accepted")
		}
	}
}
func FuzzSafePath(f *testing.F) {
	for _, s := range []string{"../outside", "repo.git", "a//b", "link/repo.git", ".hidden", "a\\b"} {
		f.Add(s)
	}
	root := f.TempDir()
	root, err := filepath.EvalSymlinks(root)
	if err != nil {
		f.Fatal(err)
	}
	f.Fuzz(func(t *testing.T, s string) {
		if len(s) > 4096 {
			return
		}
		p, e := SafePath(root, s)
		if e == nil {
			rel, e := filepath.Rel(root, p)
			if e != nil || strings.HasPrefix(rel, "../") || filepath.IsAbs(rel) {
				t.Fatalf("escaped root: %q", s)
			}
		}
	})
}

func TestRealResticEncryptedRecovery(t *testing.T) {
	restic := os.Getenv("CGIT_TEST_RESTIC")
	if restic == "" {
		t.Skip("set CGIT_TEST_RESTIC to run a real encrypted recovery")
	}
	c, work := fixture(t)
	root := filepath.Dir(c.Repos)
	root, e := filepath.EvalSymlinks(root)
	if e != nil {
		t.Fatal(e)
	}
	payload := []byte("encrypted LFS recovery bytes")
	oid := hash(payload)
	write(t, filepath.Join(work, "asset.bin"), fmt.Sprintf("version https://git-lfs.github.com/spec/v1\noid sha256:%s\nsize %d\n", oid, len(payload)))
	testGit(t, "-C", work, "add", ".")
	testGit(t, "-C", work, "commit", "-m", "recovery pointer")
	testGit(t, "-C", work, "push", filepath.Join(c.Repos, "demo.git"), "HEAD")
	write(t, filepath.Join(c.LFS, "demo.git/objects", oid[:2], oid[2:4], oid), string(payload))
	write(t, filepath.Join(c.Access, ".gitolite/conf/gitolite.conf-compiled.pm"), "compiled policy\n")
	write(t, filepath.Join(c.Repos, "demo.git/gl-conf"), "repository policy\n")
	write(t, filepath.Join(c.Access, "maintenance"), "fixture\n")
	users := map[string]User{"owner": {Enabled: true, Owner: true}}
	if _, e = snapshot(c.Access, c.Repos, users); e != nil {
		t.Fatal(e)
	}
	write(t, filepath.Join(c.Access, "ssh/authorized_keys"), "forced command fixture\n")
	if _, e = tokenSecret(c.Access); e != nil {
		t.Fatal(e)
	}
	os.Remove(filepath.Join(c.Access, "maintenance"))
	tools := t.TempDir()
	if e = os.Symlink(restic, filepath.Join(tools, "restic")); e != nil {
		t.Fatal(e)
	}
	t.Setenv("PATH", tools+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("RESTIC_CACHE_DIR", filepath.Join(t.TempDir(), "cache"))
	password := filepath.Join(t.TempDir(), "password")
	write(t, password, "synthetic recovery password\n")
	repo := filepath.Join(t.TempDir(), "restic")
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	result, e := backup(ctx, c, Job{ID: "real", Repository: repo, PasswordFile: password})
	if e != nil {
		t.Fatal(e)
	}
	id, ok := result["snapshot"].(string)
	if !ok || id == "" {
		t.Fatal(result)
	}
	dst := filepath.Join(t.TempDir(), "restored")
	os.Mkdir(dst, 0700)
	base, e := resticArgs(repo, password)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = command(ctx, resticEnv(), nil, restic, append(base, "restore", id+":"+root, "--target", dst, "--verify")...); e != nil {
		t.Fatal(e)
	}
	if n, e := verifyRestored(ctx, dst); e != nil || n != 1 {
		t.Fatal(n, e)
	}
	got, e := os.ReadFile(filepath.Join(dst, "lfs/demo.git/objects", oid[:2], oid[2:4], oid))
	if e != nil || !bytes.Equal(got, payload) {
		t.Fatal("restored bytes differ", e)
	}
	clone := filepath.Join(t.TempDir(), "client")
	testGit(t, "clone", filepath.Join(dst, "repos/demo.git"), clone)
	if got, e := os.ReadFile(filepath.Join(clone, "README.md")); e != nil || string(got) != "# fixture\n" {
		t.Fatal(e)
	}
	wrong := filepath.Join(t.TempDir(), "wrong")
	write(t, wrong, "wrong password\n")
	wrongBase, _ := resticArgs(repo, wrong)
	if _, e = command(ctx, resticEnv(), nil, restic, append(wrongBase, "snapshots", "--json")...); e == nil {
		t.Fatal("wrong password accepted")
	}
	write(t, filepath.Join(dst, "lfs/demo.git/objects", oid[:2], oid[2:4], oid), strings.Repeat("x", len(payload)))
	if _, e = verifyRestored(ctx, dst); e == nil {
		t.Fatal("restored corruption accepted")
	}
}

func TestStrictConfigurationTypes(t *testing.T) {
	for _, input := range []string{`{"owner":{"enabled":true,"owner":true},"reader":{"enabled":null}}`, `{"owner":{"enabled":true,"owner":null}}`, `{"owner":{"enabled":true,"owner":true,"extra":"ignored"}}`} {
		p := filepath.Join(t.TempDir(), "users.json")
		write(t, p, input)
		if _, e := readUsers(p); e == nil {
			t.Errorf("invalid identity accepted: %s", input)
		}
	}
	for _, field := range []string{`"enabled":null`, `"enabled":true,"timeout":0`, `"enabled":true,"retain_count":0`, `"enabled":true,"destination":null`} {
		p := filepath.Join(t.TempDir(), "jobs.json")
		write(t, p, `{"schema_version":1,"timezone":"UTC","jobs":[{"id":"fixture","type":"archive","cron":"0 3 * * *",`+field+`}]}`)
		if _, e := loadJobs(p); e == nil {
			t.Errorf("invalid job accepted: %s", field)
		}
	}
}
