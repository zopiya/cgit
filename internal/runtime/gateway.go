package runtime

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"unicode"
	"unicode/utf8"
)

const jsonType = "application/vnd.git-lfs+json"

type gateway struct {
	c           *Config
	access      *Access
	user, token string
	sent        bool
	locks       []*os.File
}

func (g *gateway) header(status int, typ string, h map[string]string) {
	g.sent = true
	fmt.Fprintf(os.Stdout, "Status: %d\r\nContent-Type: %s\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n", status, typ)
	for k, v := range h {
		fmt.Fprintf(os.Stdout, "%s: %s\r\n", k, v)
	}
	fmt.Fprint(os.Stdout, "\r\n")
}
func (g *gateway) respond(status int, v any) {
	b := jsonBytes(v)
	h := map[string]string{"Content-Length": strconv.Itoa(len(b))}
	if status == 401 {
		h["WWW-Authenticate"] = `Basic realm="cgit", charset="UTF-8"`
	}
	g.header(status, jsonType, h)
	if os.Getenv("REQUEST_METHOD") != "HEAD" {
		os.Stdout.Write(b)
	}
}
func (g *gateway) auth() error {
	if g.c.AuthMode == "access" {
		a, e := NewAccess(g.c)
		if e != nil {
			return e
		}
		g.access = a
	}
	s := os.Getenv("HTTP_AUTHORIZATION")
	if s == "" {
		if g.c.AuthMode == "private" {
			return fail(401, "Authentication required")
		}
		return nil
	}
	if strings.HasPrefix(s, "Bearer ") {
		if g.access == nil {
			return fail(401, "Authentication required")
		}
		g.token = strings.TrimPrefix(s, "Bearer ")
		if g.token == "" || strings.IndexFunc(g.token, unicode.IsSpace) >= 0 {
			return fail(401, "Authentication required")
		}
		return nil
	}
	if !strings.HasPrefix(s, "Basic ") {
		return fail(401, "Authentication required")
	}
	b, e := base64.StdEncoding.Strict().DecodeString(strings.TrimPrefix(s, "Basic "))
	if e != nil {
		return fail(401, "Authentication required")
	}
	user, password, ok := bytes.Cut(b, []byte(":"))
	if !ok || !utf8.Valid(user) {
		return fail(401, "Authentication required")
	}
	valid := false
	if g.access != nil {
		valid = g.access.Authenticate(string(user), password)
	} else {
		secret, e := os.ReadFile(g.c.PasswordFile)
		if e != nil {
			return fail(503, "Authentication configuration unavailable")
		}
		valid = g.c.Username != "" && hmac.Equal(user, []byte(g.c.Username)) && hmac.Equal(password, bytes.TrimRight(secret, "\r\n"))
	}
	if !valid {
		return fail(401, "Authentication required")
	}
	g.user = string(user)
	return nil
}
func (g *gateway) require(name, op, surface string) error {
	user := g.user
	if g.token != "" {
		if g.access == nil || surface != "lfs" {
			return fail(401, "Authentication required")
		}
		tokenOp := "download"
		if op == "W" {
			tokenOp = "upload"
		}
		var e error
		user, e = verifyToken(g.c, g.token, strings.TrimSuffix(name, ".git"), tokenOp, g.access.State.Generation)
		if e != nil {
			return e
		}
	}
	if g.access != nil {
		return g.access.Require(name, user, op)
	}
	if op == "W" {
		if g.c.Username == "" {
			return fail(403, "HTTP writes require a configured account")
		}
		if user == "" {
			return fail(401, "Authentication required")
		}
	}
	if op == "W" && len(g.locks) == 0 {
		f, e := holdShared(writeRoot(g.c))
		if e != nil {
			return e
		}
		g.locks = append(g.locks, f)
	}
	return nil
}
func contentLength(limit int64) (int64, error) {
	s := os.Getenv("CONTENT_LENGTH")
	if !regexp.MustCompile(`^[0-9]+$`).MatchString(s) {
		return 0, fail(411, "Content-Length required")
	}
	n, e := strconv.ParseInt(s, 10, 64)
	if e != nil || n > limit {
		return 0, fail(413, "Request exceeds configured size limit")
	}
	return n, nil
}
func readRequestJSON() (map[string]json.RawMessage, error) {
	typ := strings.TrimSpace(strings.SplitN(os.Getenv("CONTENT_TYPE"), ";", 2)[0])
	if typ != jsonType && typ != "application/json" {
		return nil, fail(415, "Expected Git LFS JSON")
	}
	n, e := contentLength(1 << 20)
	if e != nil {
		return nil, e
	}
	b := make([]byte, n)
	if _, e = io.ReadFull(os.Stdin, b); e != nil {
		return nil, fail(400, "Incomplete request")
	}
	var v map[string]json.RawMessage
	if e = json.Unmarshal(b, &v); e != nil {
		return nil, fail(400, "Invalid JSON")
	}
	if v == nil {
		return nil, fail(422, "Expected a JSON object")
	}
	return v, nil
}
func rawString(v json.RawMessage) string { var s string; json.Unmarshal(v, &s); return s }
func objectInfo(v json.RawMessage, max int64) (string, int64, error) {
	var obj struct {
		OID  string `json:"oid"`
		Size *int64 `json:"size"`
	}
	if e := json.Unmarshal(v, &obj); e != nil || obj.Size == nil || *obj.Size < 0 || !oidRE.MatchString(obj.OID) {
		return "", 0, fail(422, "Invalid object")
	}
	if *obj.Size > max {
		return "", 0, fail(413, "Object exceeds configured size limit")
	}
	return obj.OID, *obj.Size, nil
}
func (g *gateway) origin() (string, error) {
	if g.c.BaseURL != "" {
		return g.c.BaseURL, nil
	}
	s := os.Getenv("HTTP_HOST")
	if !regexp.MustCompile(`^(?:[A-Za-z0-9.\-]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?$`).MatchString(s) {
		return "", fail(400, "Invalid Host header")
	}
	return "http://" + s, nil
}
func escapePath(s string) string {
	parts := strings.Split(s, "/")
	for i := range parts {
		parts[i] = url.PathEscape(parts[i])
	}
	return strings.Join(parts, "/")
}
func (g *gateway) batch(name string) error {
	v, e := readRequestJSON()
	if e != nil {
		return e
	}
	op := rawString(v["operation"])
	if op != "upload" && op != "download" {
		return fail(422, "Unsupported operation")
	}
	permission := "R"
	if op == "upload" {
		permission = "W"
	}
	if e = g.require(name, permission, "lfs"); e != nil {
		return e
	}
	if b, ok := v["hash_algo"]; ok && rawString(b) != "sha256" {
		return fail(422, "Only SHA-256 is supported")
	}
	if b, ok := v["transfers"]; ok {
		var ts []string
		if json.Unmarshal(b, &ts) != nil || !contains(ts, "basic") {
			return fail(422, "Only basic transfer is supported")
		}
	}
	var objects []json.RawMessage
	if json.Unmarshal(v["objects"], &objects) != nil || objects == nil || len(objects) > 1000 {
		return fail(422, "Expected at most 1000 objects")
	}
	origin, e := g.origin()
	if e != nil {
		return e
	}
	results := []any{}
	for _, obj := range objects {
		oid, size, e := objectInfo(obj, g.c.MaxSize)
		if e != nil {
			return e
		}
		p, e := ObjectPath(g.c, name, oid)
		if e != nil {
			return e
		}
		item := map[string]any{"oid": oid, "size": size, "authenticated": g.token != ""}
		href := origin + "/" + escapePath(name) + "/info/lfs/objects/" + oid
		action := func(h string) map[string]any {
			a := map[string]any{"href": h}
			if g.token != "" {
				a["header"] = map[string]string{"Authorization": "Bearer " + g.token}
			}
			return a
		}
		st, se := os.Stat(p)
		has := se == nil && st.Mode().IsRegular()
		if has && st.Size() != size {
			item["error"] = map[string]any{"code": 422, "message": "Stored size does not match pointer"}
		} else if op == "download" {
			if has {
				item["actions"] = map[string]any{"download": action(href)}
			} else {
				item["error"] = map[string]any{"code": 404, "message": "LFS object is missing"}
			}
		} else if !has {
			item["actions"] = map[string]any{"upload": action(href + "?size=" + strconv.FormatInt(size, 10)), "verify": action(href + "/verify")}
		}
		results = append(results, item)
	}
	g.respond(200, map[string]any{"transfer": "basic", "objects": results, "hash_algo": "sha256"})
	return nil
}
func (g *gateway) upload(name, oid string, q url.Values) error {
	if e := g.require(name, "W", "lfs"); e != nil {
		return e
	}
	size, e := contentLength(g.c.MaxSize)
	if e != nil {
		return e
	}
	if len(q["size"]) != 1 || q.Get("size") != strconv.FormatInt(size, 10) {
		return fail(422, "Upload size differs from batch request")
	}
	dst, e := ObjectPath(g.c, name, oid)
	if e != nil {
		return e
	}
	if e = os.MkdirAll(filepath.Dir(dst), 0755); e != nil {
		return e
	}
	if e = g.c.checkUploadSpace(filepath.Dir(dst), size); e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(dst), ".upload-")
	if e != nil {
		return e
	}
	defer os.Remove(f.Name())
	defer f.Close()
	h := sha256.New()
	n, e := io.CopyN(io.MultiWriter(f, h), os.Stdin, size)
	if e != nil || n != size {
		return fail(400, "Incomplete upload")
	}
	if fmt.Sprintf("%x", h.Sum(nil)) != oid {
		return fail(422, "Upload SHA-256 does not match object ID")
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
	if e = os.Rename(f.Name(), dst); e != nil {
		return e
	}
	if e = syncDir(filepath.Dir(dst)); e != nil {
		return e
	}
	g.respond(200, map[string]any{})
	return nil
}
func (g *gateway) sendObject(name, oid, filename string, inline bool, expected int64) error {
	p, e := ObjectPath(g.c, name, oid)
	if e != nil {
		return e
	}
	f, e := os.Open(p)
	if os.IsNotExist(e) {
		return fail(404, "LFS object is missing; upload or migrate it first")
	}
	if e != nil {
		return e
	}
	defer f.Close()
	st, e := f.Stat()
	if e != nil {
		return e
	}
	if !st.Mode().IsRegular() {
		return fail(404, "Not found")
	}
	size := st.Size()
	if expected >= 0 && size != expected {
		return fail(422, "Stored size does not match pointer")
	}
	start, end, status := int64(0), size-1, 200
	etag := `"` + oid + `"`
	h := map[string]string{"Accept-Ranges": "bytes", "ETag": etag}
	r := os.Getenv("HTTP_RANGE")
	if ir := os.Getenv("HTTP_IF_RANGE"); ir != "" && ir != etag {
		r = ""
	}
	if r != "" {
		m := regexp.MustCompile(`^bytes=([0-9]*)-([0-9]*)$`).FindStringSubmatch(r)
		invalid := m == nil || size == 0
		if !invalid {
			invalid = m[1] == "" && m[2] == ""
			if m[1] != "" {
				var er error
				start, er = strconv.ParseInt(m[1], 10, 64)
				invalid = invalid || er != nil
				if m[2] != "" {
					v, er := strconv.ParseInt(m[2], 10, 64)
					invalid = invalid || er != nil
					if v < end {
						end = v
					}
				}
			} else if m[2] != "" {
				v, er := strconv.ParseInt(m[2], 10, 64)
				invalid = invalid || er != nil || v == 0
				start = size - v
				if start < 0 {
					start = 0
				}
			}
		}
		if invalid || start > end || start >= size {
			g.header(416, jsonType, map[string]string{"Content-Range": fmt.Sprintf("bytes */%d", size), "Content-Length": "0"})
			return nil
		}
		status = 206
		h["Content-Range"] = fmt.Sprintf("bytes %d-%d/%d", start, end, size)
	}
	filename = filepath.Base(filename)
	if filename == "." || filename == "/" || filename == "" {
		filename = oid
	}
	types := map[string]string{".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf"}
	typ, disp := "application/octet-stream", "attachment"
	if inline && types[strings.ToLower(filepath.Ext(filename))] != "" {
		typ = types[strings.ToLower(filepath.Ext(filename))]
		disp = "inline"
	}
	h["Content-Disposition"] = disp + "; filename*=UTF-8''" + url.PathEscape(filename)
	h["Content-Security-Policy"] = "sandbox; default-src 'none'"
	h["Content-Length"] = strconv.FormatInt(end-start+1, 10)
	g.header(status, typ, h)
	if os.Getenv("REQUEST_METHOD") != "HEAD" {
		if _, e = f.Seek(start, 0); e != nil {
			return e
		}
		_, e = io.CopyN(os.Stdout, f, end-start+1)
	}
	return e
}
func (g *gateway) lfs(name, action, method string, q url.Values) error {
	_, name, e := repository(g.c, name, true)
	if e != nil {
		return e
	}
	if action == "objects/batch" && method == "POST" {
		return g.batch(name)
	}
	m := regexp.MustCompile(`^objects/([a-f0-9]{64})(/verify)?$`).FindStringSubmatch(action)
	if m != nil {
		if m[2] != "" && method == "POST" {
			if e = g.require(name, "W", "lfs"); e != nil {
				return e
			}
			v, e := readRequestJSON()
			if e != nil {
				return e
			}
			oid, size, e := objectInfo(jsonBytes(v), g.c.MaxSize)
			if e != nil {
				return e
			}
			p, e := ObjectPath(g.c, name, m[1])
			if e != nil {
				return e
			}
			st, e := os.Stat(p)
			if e != nil || oid != m[1] || !st.Mode().IsRegular() || st.Size() != size {
				return fail(422, "Object verification failed")
			}
			g.respond(200, map[string]any{})
			return nil
		}
		if m[2] == "" {
			if method == "PUT" {
				return g.upload(name, m[1], q)
			}
			if method == "GET" || method == "HEAD" {
				if e = g.require(name, "R", "lfs"); e != nil {
					return e
				}
				return g.sendObject(name, m[1], q.Get("filename"), q.Get("inline") == "1", -1)
			}
		}
	}
	if strings.HasPrefix(action, "locks") {
		return fail(501, "LFS locking is not supported by this personal server")
	}
	return fail(405, "Unsupported LFS endpoint or method")
}
func inheritedCGI(env []string) []string {
	allowed := strings.Fields("REQUEST_METHOD REQUEST_URI QUERY_STRING SERVER_PROTOCOL SERVER_NAME SERVER_PORT SERVER_ADDR REMOTE_ADDR REMOTE_PORT GATEWAY_INTERFACE CONTENT_TYPE CONTENT_LENGTH PATH_INFO SCRIPT_NAME DOCUMENT_ROOT HTTP_HOST HTTP_USER_AGENT HTTP_ACCEPT HTTP_ACCEPT_ENCODING HTTP_RANGE HTTP_IF_RANGE HTTP_IF_NONE_MATCH HTTP_IF_MODIFIED_SINCE HTTP_GIT_PROTOCOL")
	for _, k := range allowed {
		if v, ok := os.LookupEnv(k); ok {
			env = setEnv(env, k, v)
		}
	}
	return env
}
func execProgram(name string, args, env []string) error {
	path, e := exec.LookPath(name)
	if e != nil {
		return e
	}
	return syscall.Exec(path, append([]string{name}, args...), env)
}
func (g *gateway) smart(name, action, method string, q url.Values) error {
	repo, name, e := repository(g.c, name, true)
	if e != nil {
		return e
	}
	service := action
	if action == "info/refs" {
		if method != "GET" || len(q["service"]) != 1 || !contains([]string{"git-upload-pack", "git-receive-pack"}, q.Get("service")) {
			return fail(400, "Invalid Git service request")
		}
		service = q.Get("service")
	} else if method != "POST" {
		return fail(405, "Git RPC requires POST")
	}
	op := "R"
	if service == "git-receive-pack" {
		if !g.c.HTTPPush {
			return fail(403, "HTTP push is disabled; use SSH or enable CGIT_HTTP_PUSH")
		}
		op = "W"
	}
	if e = g.require(name, op, "git"); e != nil {
		return e
	}
	env := cleanEnv("")
	if g.access != nil {
		u := g.user
		if u == "" {
			u = "anonymous"
		}
		env, e = g.access.GitEnv(name, u)
		if e != nil {
			return e
		}
		env = append(env, "REMOTE_USER="+u)
	} else if g.user != "" {
		env = append(env, "REMOTE_USER="+g.user)
	}
	env = inheritedCGI(env)
	env = setEnv(env, "PATH_INFO", "/"+name+"/"+action)
	env = append(env, "GIT_PROJECT_ROOT="+g.c.Repos, "GIT_HTTP_EXPORT_ALL=1", "GIT_PROTOCOL="+os.Getenv("HTTP_GIT_PROTOCOL"))
	g.inheritLocks()
	env = g.c.transportEnv(env)
	return execProgram("git", []string{"-c", "safe.directory=" + repo, "-c", fmt.Sprintf("http.receivepack=%t", g.c.HTTPPush), "http-backend"}, env)
}
func (g *gateway) inheritLocks() {
	for _, f := range g.locks {
		syscall.Syscall(syscall.SYS_FCNTL, f.Fd(), syscall.F_SETFD, 0)
	}
	if g.access != nil && g.access.lock != nil {
		syscall.Syscall(syscall.SYS_FCNTL, g.access.lock.Fd(), syscall.F_SETFD, 0)
	}
}
func (g *gateway) plain(path string, q url.Values) (bool, error) {
	parts := strings.SplitN(strings.TrimLeft(path, "/"), "/plain/", 2)
	if len(parts) != 2 {
		return false, nil
	}
	repo, name, e := repository(g.c, parts[0], true)
	if e != nil {
		return true, e
	}
	if e = g.require(name, "R", "lfs"); e != nil {
		return true, e
	}
	revision := q.Get("id")
	if revision == "" {
		revision = q.Get("h")
	}
	if revision == "" {
		revision = "HEAD"
	}
	commit, e := git(repo, "rev-parse", "--verify", "--end-of-options", revision+"^{commit}")
	if e != nil {
		return false, nil
	}
	spec := strings.TrimSpace(string(commit)) + ":" + parts[1]
	size, e := git(repo, "cat-file", "-s", spec)
	if e != nil {
		return false, nil
	}
	n, e := strconv.ParseInt(strings.TrimSpace(string(size)), 10, 64)
	if e != nil || n > 1024 {
		return false, nil
	}
	b, e := git(repo, "cat-file", "blob", spec)
	if e != nil {
		return false, nil
	}
	oid, n, ok := parsePointer(b)
	if !ok {
		return false, nil
	}
	return true, g.sendObject(name, oid, parts[1], true, n)
}
func parsePointer(b []byte) (string, int64, bool) {
	if len(b) > 1024 {
		return "", 0, false
	}
	m := pointerRE.FindSubmatch(b)
	if m == nil {
		return "", 0, false
	}
	n, e := strconv.ParseInt(string(m[2]), 10, 64)
	return string(m[1]), n, e == nil
}
func (g *gateway) web(path string, q url.Values) error {
	if _, e := scan(g.c.Repos); e != nil {
		return e
	}
	if g.c.Policy.ProjectList != "" {
		if _, e := projectEntries(g.c.Policy.ProjectList); e != nil {
			return e
		}
	}
	env := inheritedCGI(cleanEnv(""))
	config := g.c.CgitConfig
	if g.access != nil {
		if g.token != "" {
			return fail(401, "Authentication required")
		}
		target := strings.Trim(path, "/")
		for _, k := range []string{"r", "url"} {
			if v, ok := q[k]; ok {
				if len(v) != 1 {
					return fail(400, "Ambiguous repository URL")
				}
				target = v[0]
				if k == "url" {
					u, e := url.Parse(target)
					if e != nil {
						return e
					}
					if u.Scheme != "" || u.Host != "" {
						target = ""
					} else {
						target = u.Path
					}
				}
				break
			}
		}
		names := []string{}
		for n := range g.access.State.Repositories {
			names = append(names, n)
		}
		sort.Slice(names, func(i, j int) bool { return len(names[i]) > len(names[j]) })
		selected := false
		for _, n := range names {
			matched := false
			for _, alias := range []string{n, n + ".git"} {
				if target == alias || strings.HasPrefix(target, alias+"/") {
					if e := g.access.Require(n, g.user, "R"); e != nil {
						return e
					}
					matched = true
				}
			}
			if matched {
				// A repository page only needs this repository in cgit's scan.
				// The root index still checks every repository for this identity.
				names = []string{n}
				selected = true
				break
			}
		}
		visible := []string{}
		for _, n := range names {
			ok := selected // Require already checked this exact target in this request.
			if !selected {
				var e error
				ok, e = g.access.Allowed(n, g.user, "R")
				if e != nil {
					return e
				}
			}
			if ok {
				_, _, e := repository(g.c, n, true)
				if e == nil {
					visible = append(visible, n+".git")
				} else {
					var re *Error
					if !errors.As(e, &re) || re.Status != 404 {
						return e
					}
				}
			}
		}
		sort.Strings(visible)
		dir, e := os.MkdirTemp(filepath.Dir(filepath.Dir(config)), "policies-")
		if e != nil {
			return e
		} // cgit runs in a child so its private policy files are removed on completion.
		defer os.RemoveAll(dir)
		project := filepath.Join(dir, "projects")
		if e = os.WriteFile(project, []byte(strings.Join(visible, "\n")+"\n"), 0600); e != nil {
			return e
		}
		b, e := os.ReadFile(config)
		if e != nil {
			return e
		}
		s := regexp.MustCompile(`(?m)^project-list=.*\n?`).ReplaceAllString(string(b), "")
		s = strings.Replace(s, "scan-path=", "project-list="+project+"\nscan-path=", 1)
		config = filepath.Join(dir, "cgitrc")
		if e = os.WriteFile(config, []byte(s), 0600); e != nil {
			return e
		}
	}
	env = setEnv(env, "CGIT_CONFIG", config)
	env = setEnv(env, "CGIT_RUNTIME_CONFIG", configPath())
	env = setEnv(env, "CGIT_PERSONAL_USER", g.user)
	env = setEnv(env, "PATH_INFO", path)
	env = setEnv(env, "SCRIPT_NAME", "")
	cmd := exec.Command(g.c.Cgit)
	cmd.Env = env
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}
func (g *gateway) run() error {
	defer func() {
		for _, f := range g.locks {
			f.Close()
		}
	}()
	if e := g.auth(); e != nil {
		return e
	}
	if g.access != nil {
		defer g.access.Close()
	}
	method := envDefault("REQUEST_METHOD", "GET")
	u, e := url.ParseRequestURI(envDefault("REQUEST_URI", "/"))
	if e != nil {
		return fail(400, "Invalid URL")
	}
	path := u.Path
	if !utf8.ValidString(path) || strings.IndexFunc(path, func(r rune) bool { return r < 32 || r == 127 || r == '\\' }) >= 0 {
		return fail(400, "Invalid path")
	}
	for _, p := range strings.Split(path, "/") {
		if p == "." || p == ".." {
			return fail(404, "Not found")
		}
	}
	q, e := url.ParseQuery(u.RawQuery)
	if e != nil {
		return fail(400, "Invalid query")
	}
	if path == "/_login" {
		if g.access == nil {
			return fail(404, "Not found")
		}
		if method != "GET" && method != "HEAD" {
			return fail(405, "Method not allowed")
		}
		if g.user == "" || g.token != "" {
			return fail(401, "Authentication required")
		}
		g.header(303, "text/plain; charset=UTF-8", map[string]string{"Location": "/", "Content-Length": "0"})
		return nil
	}
	if path == "/_health" {
		if method != "GET" && method != "HEAD" {
			return fail(405, "Method not allowed")
		}
		if g.access != nil && g.user == "" {
			return fail(401, "Authentication required")
		}
		if !isDir(g.c.Repos) || !isDir(g.c.LFS) || !executable(g.c.Cgit) {
			g.respond(503, map[string]string{"status": "unavailable"})
		} else {
			g.respond(200, map[string]string{"status": "ok"})
		}
		return nil
	}
	if m := regexp.MustCompile(`^/(.+?)/info/lfs/(.*)$`).FindStringSubmatch(path); m != nil {
		return g.lfs(m[1], m[2], method, q)
	}
	if m := regexp.MustCompile(`^/(.+?)/(info/refs|git-upload-pack|git-receive-pack)$`).FindStringSubmatch(path); m != nil && (m[2] != "info/refs" || q.Has("service")) {
		return g.smart(m[1], m[2], method, q)
	}
	if method != "GET" && method != "HEAD" {
		return fail(405, "Method not allowed")
	}
	if handled, e := g.plain(path, q); handled || e != nil {
		return e
	}
	return g.web(path, q)
}
func Gateway() error {
	c, e := LoadConfig()
	g := &gateway{c: c}
	if e == nil {
		e = g.run()
	}
	if e != nil {
		if !g.sent {
			status, msg := 500, "Server error; check container logs and cgit-doctor"
			var re *Error
			if errors.As(e, &re) {
				status, msg = re.Status, re.Message
			}
			g.respond(status, map[string]string{"message": msg})
		}
		fmt.Fprintln(os.Stderr, "cgit gateway:", e)
	}
	return nil
}
