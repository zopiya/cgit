package runtime

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/pbkdf2"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"
	"unicode"
)

type User struct {
	Enabled    bool     `json:"enabled"`
	Owner      bool     `json:"owner,omitempty"`
	SecretHash string   `json:"secret_hash,omitempty"`
	Keys       []string `json:"keys,omitempty"`
}
type Snapshot struct {
	Schema       int               `json:"schema_version"`
	Users        map[string]User   `json:"users"`
	Repositories map[string]string `json:"repositories"`
	CommonDigest string            `json:"common_digest"`
	Generation   string            `json:"generation"`
}
type Access struct {
	Cfg    *Config
	State  Snapshot
	Root   string
	Binary string
	lock   *os.File
}

func canonical(v any) []byte {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	if err := e.Encode(v); err != nil {
		panic(err)
	}
	raw := strings.TrimSuffix(b.String(), "\n")
	var out strings.Builder
	for _, r := range raw {
		if r > 127 {
			if r > 65535 {
				r -= 65536
				fmt.Fprintf(&out, "\\u%04x\\u%04x", 0xd800+(r>>10), 0xdc00+(r&1023))
			} else {
				fmt.Fprintf(&out, "\\u%04x", r)
			}
		} else {
			out.WriteRune(r)
		}
	}
	return []byte(out.String())
}
func canonicalObject(v any) []byte {
	var obj any
	d := json.NewDecoder(bytes.NewReader(jsonBytes(v)))
	d.UseNumber()
	if e := d.Decode(&obj); e != nil {
		panic(e)
	}
	return canonical(obj)
}

var secretRE = regexp.MustCompile(`^pbkdf2_sha256\$600000\$([a-f0-9]{32})\$([a-f0-9]{64})$`)

func hashSecret(secret []byte) (string, error) {
	if len(secret) < 16 || len(secret) > 1024 {
		return "", fmt.Errorf("credential must be 16 to 1024 bytes")
	}
	salt := make([]byte, 16)
	if _, e := rand.Read(salt); e != nil {
		return "", e
	}
	key, e := pbkdf2.Key(sha256.New, string(secret), salt, 600000, 32)
	if e != nil {
		return "", e
	}
	return fmt.Sprintf("pbkdf2_sha256$600000$%x$%x", salt, key), nil
}
func verifySecret(secret []byte, encoded string) bool {
	m := secretRE.FindStringSubmatch(encoded)
	if m == nil || len(secret) > 1024 {
		return false
	}
	salt, _ := hex.DecodeString(m[1])
	expected, _ := hex.DecodeString(m[2])
	actual, e := pbkdf2.Key(sha256.New, string(secret), salt, 600000, 32)
	return e == nil && hmac.Equal(actual, expected)
}
func validateUsers(users map[string]User) error {
	owners := 0
	for n, u := range users {
		if !userRE.MatchString(n) || n == "anonymous" || n == "root" {
			return fmt.Errorf("invalid or reserved identity")
		}
		if u.SecretHash != "" && !secretRE.MatchString(u.SecretHash) {
			return fmt.Errorf("unsupported credential hash")
		}
		for _, k := range u.Keys {
			if !strings.HasPrefix(k, "SHA256:") || len(k) > 128 || strings.IndexFunc(k, unicode.IsSpace) >= 0 {
				return fmt.Errorf("invalid key fingerprint")
			}
		}
		if u.Enabled && u.Owner {
			owners++
		}
	}
	if owners == 0 {
		return fmt.Errorf("at least one enabled owner required")
	}
	return nil
}
func readUsers(p string) (map[string]User, error) {
	b, e := os.ReadFile(p)
	if e != nil {
		return nil, e
	}
	if len(b) > 1<<20 {
		return nil, fmt.Errorf("identity file too large")
	}
	var users map[string]User
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if e = d.Decode(&users); e != nil {
		return nil, e
	}
	return users, validateUsers(users)
}
func loadSnapshot(root string) (Snapshot, error) {
	var s Snapshot
	var raw map[string]json.RawMessage
	if e := readJSON(filepath.Join(root, "identity.json"), &raw); e != nil {
		return s, e
	}
	b := jsonBytes(raw)
	if e := json.Unmarshal(b, &s); e != nil {
		return s, e
	}
	delete(raw, "generation")
	if s.Schema != 1 || !oidRE.MatchString(s.Generation) || hash(canonicalObject(raw)) != s.Generation {
		return s, fmt.Errorf("identity snapshot fingerprint is invalid")
	}
	if e := validateUsers(s.Users); e != nil {
		return s, e
	}
	if s.Repositories == nil {
		return s, fmt.Errorf("missing repository manifest")
	}
	for n, v := range s.Repositories {
		if !repoRE.MatchString(n) || n == "gitolite-admin" || !oidRE.MatchString(v) {
			return s, fmt.Errorf("invalid repository manifest")
		}
	}
	return s, nil
}
func checkDigest(p, expected string) error {
	v, e := digest(p)
	if e != nil {
		return e
	}
	if !oidRE.MatchString(expected) || v != expected {
		return fmt.Errorf("compiled policy fingerprint mismatch")
	}
	return nil
}
func NewAccess(c *Config) (*Access, error) {
	if c.Access == nil || !filepath.IsAbs(c.Access.Root) || !filepath.IsAbs(c.Access.Gitolite) || !executable(c.Access.Gitolite) {
		return nil, fail(503, "Access configuration unavailable")
	}
	a := &Access{Cfg: c, Root: c.Access.Root, Binary: c.Access.Gitolite}
	if e := a.current(); e != nil {
		return nil, e
	}
	s, e := loadSnapshot(a.Root)
	if e != nil {
		return nil, fail(503, "Access configuration unavailable")
	}
	a.State = s
	if e = checkDigest(filepath.Join(a.Root, ".gitolite/conf/gitolite.conf-compiled.pm"), s.CommonDigest); e != nil {
		return nil, fail(503, "Access configuration unavailable")
	}
	return a, nil
}
func (a *Access) current() error {
	if exists(filepath.Join(a.Root, "maintenance")) {
		return fail(503, "Access policy is under maintenance")
	}
	return nil
}
func (a *Access) Authenticate(user string, secret []byte) bool {
	u := a.State.Users[user]
	encoded := u.SecretHash
	if encoded == "" {
		encoded = "pbkdf2_sha256$600000$" + strings.Repeat("0", 32) + "$" + strings.Repeat("0", 64)
	}
	valid := verifySecret(secret, encoded)
	return valid && u.Enabled && u.SecretHash != ""
}
func (a *Access) Allowed(name, user, op string) (bool, error) {
	if e := a.current(); e != nil {
		return false, e
	}
	name = strings.TrimSuffix(name, ".git")
	expected, ok := a.State.Repositories[name]
	if !ok || !contains([]string{"R", "W"}, op) || (user == "" && op != "R") || (user != "" && !a.State.Users[user].Enabled) {
		return false, nil
	}
	if e := checkDigest(filepath.Join(a.Root, ".gitolite/conf/gitolite.conf-compiled.pm"), a.State.CommonDigest); e != nil {
		return false, fail(503, "Authorization backend unavailable")
	}
	p, e := SafePath(a.Cfg.Repos, name+".git")
	if e != nil {
		return false, e
	}
	if e = checkDigest(filepath.Join(p, "gl-conf"), expected); e != nil {
		return false, fail(503, "Authorization backend unavailable")
	}
	if user == "" {
		user = "anonymous"
	}
	_, e = boundedCommand(cleanEnv(a.Root), nil, a.Binary, "access", "-q", name, user, op, "any")
	if e == nil {
		return true, nil
	}
	if exitCode(e) == 1 {
		return false, nil
	}
	return false, fail(503, "Authorization backend unavailable")
}
func (a *Access) Require(name, user, op string) error {
	ok, e := a.Allowed(name, user, op)
	if e != nil {
		return e
	}
	if !ok {
		if user == "" {
			return fail(401, "Authentication required")
		}
		return fail(404, "Not found")
	}
	if op == "W" && a.lock == nil {
		f, e := holdShared(a.Root)
		if e != nil {
			return e
		}
		a.lock = f
	}
	return nil
}
func (a *Access) Close() {
	if a.lock != nil {
		a.lock.Close()
		a.lock = nil
	}
}
func holdShared(root string) (*os.File, error) {
	p := filepath.Join(root, "write.lock")
	if symlink(p) {
		return nil, fail(503, "Unsafe write lock")
	}
	f, e := os.OpenFile(p, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	if e = syscall.Flock(int(f.Fd()), syscall.LOCK_SH); e != nil {
		f.Close()
		return nil, e
	}
	if exists(filepath.Join(root, "maintenance")) {
		f.Close()
		return nil, fail(503, "Writes are under maintenance")
	}
	return f, nil
}
func (a *Access) GitEnv(name, user string) ([]string, error) {
	env := cleanEnv(a.Root)
	env = append(env, "GITOLITE_HTTP_HOME="+a.Root, "GL_REPO="+strings.TrimSuffix(name, ".git"), "GL_USER="+user)
	for _, k := range []string{"GL_BINDIR", "GL_LIBDIR"} {
		b, e := boundedCommand(cleanEnv(a.Root), nil, a.Binary, "query-rc", k)
		if e != nil {
			return nil, e
		}
		v := strings.TrimSpace(string(b))
		if !filepath.IsAbs(v) || !isDir(v) || strings.Contains(v, "\n") {
			return nil, fail(503, "Gitolite runtime unavailable")
		}
		env = setEnv(env, k, v)
	}
	return env, nil
}
func snapshot(root, repos string, users map[string]User) (string, error) {
	if !filepath.IsAbs(root) || !filepath.IsAbs(repos) || !regular(filepath.Join(root, "maintenance")) {
		return "", fmt.Errorf("absolute roots and maintenance required")
	}
	if e := validateUsers(users); e != nil {
		return "", e
	}
	common, e := digest(filepath.Join(root, ".gitolite/conf/gitolite.conf-compiled.pm"))
	if e != nil {
		return "", e
	}
	inventory := map[string]string{}
	e = filepath.WalkDir(repos, func(p string, d os.DirEntry, e error) error {
		if e != nil {
			return e
		}
		if d.Type()&os.ModeSymlink != 0 {
			if isDir(p) || d.Name() == "gl-conf" {
				return fmt.Errorf("symlink in repository tree")
			}
			return nil
		}
		if d.Name() != "gl-conf" {
			return nil
		}
		rel, e := filepath.Rel(repos, filepath.Dir(p))
		if e != nil {
			return e
		}
		if !strings.HasSuffix(rel, ".git") {
			return fmt.Errorf("invalid compiled path")
		}
		n := strings.TrimSuffix(filepath.ToSlash(rel), ".git")
		if n == "gitolite-admin" {
			return nil
		}
		if !repoRE.MatchString(n) {
			return fmt.Errorf("invalid repository name")
		}
		v, e := digest(p)
		inventory[n] = v
		return e
	})
	if e != nil {
		return "", e
	}
	obj := map[string]any{"schema_version": 1, "users": users, "common_digest": common, "repositories": inventory}
	generation := hash(canonicalObject(obj))
	obj["generation"] = generation
	return generation, atomicWrite(filepath.Join(root, "identity.json"), append(canonicalObject(obj), '\n'), 0600)
}
func renderKeys(root, bin string, users map[string]User) (int, error) {
	if !regular(filepath.Join(root, "maintenance")) || !filepath.IsAbs(bin) || strings.ContainsAny(bin, " ,\"'\t\n") {
		return 0, fmt.Errorf("maintenance and safe absolute forced command required")
	}
	if e := validateUsers(users); e != nil {
		return 0, e
	}
	keydir := filepath.Join(root, ".gitolite/keydir")
	if symlink(keydir) || !isDir(keydir) {
		return 0, fmt.Errorf("invalid keydir")
	}
	keys := []string{}
	seen := map[string]bool{}
	owner := false
	e := filepath.WalkDir(keydir, func(p string, d os.DirEntry, e error) error {
		if e != nil {
			return e
		}
		if d.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("symlink key path")
		}
		if d.IsDir() || !strings.HasSuffix(p, ".pub") {
			return nil
		}
		name := strings.SplitN(strings.TrimSuffix(d.Name(), ".pub"), "@", 2)[0]
		u, ok := users[name]
		if !ok || !u.Enabled {
			return fmt.Errorf("key not owned by enabled identity")
		}
		b, e := os.ReadFile(p)
		if e != nil {
			return e
		}
		line := strings.TrimSuffix(string(b), "\n")
		if len(b) > 16384 || line == "" || strings.IndexFunc(line, func(r rune) bool { return r < 32 }) >= 0 {
			return fmt.Errorf("invalid key file")
		}
		typ := strings.SplitN(line, " ", 2)[0]
		if !strings.HasPrefix(typ, "ssh-") && !strings.HasPrefix(typ, "ecdsa-") && !strings.HasPrefix(typ, "sk-") {
			return fmt.Errorf("unsupported SSH public key type")
		}
		out, e := boundedCommand(cleanEnv(""), nil, "ssh-keygen", "-lf", p, "-E", "sha256")
		if e != nil {
			return e
		}
		fields := strings.Fields(string(out))
		if len(fields) < 2 || !contains(u.Keys, fields[1]) || seen[fields[1]] {
			return fmt.Errorf("unregistered or duplicate fingerprint")
		}
		seen[fields[1]] = true
		owner = owner || u.Owner
		keys = append(keys, fmt.Sprintf("command=\"%s %s\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty,no-user-rc %s", bin, name, line))
		return nil
	})
	if e != nil {
		return 0, e
	}
	if !owner {
		return 0, fmt.Errorf("enabled owner key required")
	}
	sort.Strings(keys)
	ssh := filepath.Join(root, "ssh")
	if symlink(ssh) {
		return 0, fmt.Errorf("unsafe SSH directory")
	}
	if e = os.MkdirAll(ssh, 0700); e != nil {
		return 0, e
	}
	if e = os.Chmod(ssh, 0700); e != nil {
		return 0, e
	}
	return len(keys), atomicWrite(filepath.Join(ssh, "authorized_keys"), []byte(strings.Join(keys, "\n")+"\n"), 0600)
}
func tokenSecret(root string) ([]byte, error) {
	p := filepath.Join(root, "lfs-token.key")
	if symlink(p) {
		return nil, fmt.Errorf("unsafe token key")
	}
	if !exists(p) {
		if !regular(filepath.Join(root, "maintenance")) {
			return nil, fmt.Errorf("maintenance required to create token key")
		}
		b := make([]byte, 32)
		if _, e := rand.Read(b); e != nil {
			return nil, e
		}
		if e := atomicWrite(p, b, 0600); e != nil {
			return nil, e
		}
	}
	b, e := os.ReadFile(p)
	if e != nil {
		return nil, e
	}
	if len(b) != 32 {
		return nil, fmt.Errorf("token key must be 32 bytes")
	}
	if e = os.Chmod(p, 0600); e != nil {
		return nil, e
	}
	return b, nil
}

type tokenPayload struct {
	Audience   string `json:"aud"`
	Exp        int64  `json:"exp"`
	Generation string `json:"generation"`
	Iat        int64  `json:"iat"`
	Op         string `json:"op"`
	Repo       string `json:"repo"`
	User       string `json:"user"`
}

func issueToken(c *Config, user, repo, op, generation string) (string, error) {
	b, e := tokenSecret(c.Access.Root)
	if e != nil {
		return "", e
	}
	now := time.Now().Unix()
	payload := tokenPayload{"git-lfs", now + 300, generation, now, op, repo, user}
	raw := canonicalObject(payload)
	h := hmac.New(sha256.New, b)
	h.Write(append([]byte("v1."), raw...))
	return "v1." + base64.RawURLEncoding.EncodeToString(raw) + "." + base64.RawURLEncoding.EncodeToString(h.Sum(nil)), nil
}
func verifyToken(c *Config, token, repo, op, generation string) (string, error) {
	parts := strings.Split(token, ".")
	if len(token) > 4096 || len(parts) != 3 || parts[0] != "v1" {
		return "", fail(401, "Invalid token")
	}
	raw, e := base64.RawURLEncoding.DecodeString(parts[1])
	if e != nil {
		return "", fail(401, "Invalid token")
	}
	sig, e := base64.RawURLEncoding.DecodeString(parts[2])
	if e != nil {
		return "", fail(401, "Invalid token")
	}
	key, e := tokenSecret(c.Access.Root)
	if e != nil {
		return "", e
	}
	h := hmac.New(sha256.New, key)
	h.Write(append([]byte("v1."), raw...))
	if !hmac.Equal(sig, h.Sum(nil)) {
		return "", fail(401, "Invalid token")
	}
	var p tokenPayload
	if e = json.Unmarshal(raw, &p); e != nil {
		return "", fail(401, "Invalid token")
	}
	now := time.Now().Unix()
	if p.Audience != "git-lfs" || p.Repo != repo || p.Op != op || p.Generation != generation || p.Iat > now+60 || p.Iat > now || p.Exp <= now || p.Exp-p.Iat > 3600 || p.Exp < p.Iat || p.User == "" {
		return "", fail(401, "Invalid token scope")
	}
	return p.User, nil
}
func AccessCLI(args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("access subcommand required")
	}
	sub := args[0]
	f := flag.NewFlagSet("access", flag.ContinueOnError)
	root := f.String("directory", "", "")
	repos := f.String("repos", "", "")
	identities := f.String("identities", "", "")
	secret := f.String("secret-file", "", "")
	gl := f.String("gitolite", "", "")
	bin := f.String("command", "/usr/local/bin/cgit-ssh", "")
	sshbin := f.String("ssh-command", "/usr/local/bin/cgit-ssh", "")
	if e := f.Parse(args[1:]); e != nil {
		return e
	}
	if sub == "hash-secret" {
		b, e := os.ReadFile(*secret)
		if e != nil {
			return e
		}
		v, e := hashSecret(bytes.TrimRight(b, "\r\n"))
		if e == nil {
			fmt.Println(v)
		}
		return e
	}
	if !filepath.IsAbs(*root) {
		return fmt.Errorf("absolute directory required")
	}
	if sub == "init-lfs-token" {
		_, e := tokenSecret(*root)
		if e == nil {
			printJSON(map[string]any{"ok": true, "lfs_token_key": true, "maintenance": true})
		}
		return e
	}
	users, e := readUsers(*identities)
	if e != nil {
		return e
	}
	switch sub {
	case "snapshot":
		g, e := snapshot(*root, *repos, users)
		if e == nil {
			printJSON(map[string]any{"ok": true, "generation": g, "maintenance": true})
		}
		return e
	case "authorized-keys":
		n, e := renderKeys(*root, *bin, users)
		if e == nil {
			printJSON(map[string]any{"ok": true, "keys": n, "maintenance": true})
		}
		return e
	case "apply":
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		release, e := exclusiveFence(ctx, *root, "apply", true)
		if e != nil {
			return e
		}
		success := false
		defer func() { release(success) }()
		if _, e = command(ctx, cleanEnv(*root), nil, *gl, "compile"); e != nil {
			return e
		}
		if _, e = renderKeys(*root, *sshbin, users); e != nil {
			return e
		}
		if _, e = tokenSecret(*root); e != nil {
			return e
		}
		g, e := snapshot(*root, *repos, users)
		if e != nil {
			return e
		}
		success = true
		printJSON(map[string]any{"ok": true, "generation": g, "maintenance": false})
		return nil
	default:
		return fmt.Errorf("unknown access command")
	}
}

// Preserve the previous fail-closed JSON contract: encoding/json otherwise
// silently maps null booleans to false, hiding configuration mistakes.
func (u *User) UnmarshalJSON(data []byte) error {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	if raw == nil {
		return fmt.Errorf("identity must be an object")
	}
	for k := range raw {
		if !contains([]string{"enabled", "owner", "secret_hash", "keys"}, k) {
			return fmt.Errorf("unknown identity field: %s", k)
		}
	}
	for _, k := range []string{"enabled", "owner"} {
		v, ok := raw[k]
		if k == "enabled" && !ok {
			return fmt.Errorf("identity enabled is required")
		}
		if ok && string(v) != "true" && string(v) != "false" {
			return fmt.Errorf("identity %s must be boolean", k)
		}
	}
	if v, ok := raw["keys"]; ok && string(v) == "null" {
		return fmt.Errorf("identity keys must be a list")
	}
	if v, ok := raw["secret_hash"]; ok && string(v) == `""` {
		return fmt.Errorf("empty credential hash")
	}
	type plain User
	return json.Unmarshal(data, (*plain)(u))
}
