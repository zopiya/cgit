package runtime

import (
	"flag"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"unicode"
)

func Configure(args []string) error {
	f := flag.NewFlagSet("configure", flag.ContinueOnError)
	source := f.String("source", "/etc/cgitrc", "")
	repos := f.String("repos", "/repos", "")
	lfs := f.String("lfs", "/lfs", "")
	cgit := f.String("cgit", "/usr/local/libexec/cgit.cgi", "")
	if e := f.Parse(args); e != nil {
		return e
	}
	c := Config{Repos: *repos, LFS: *lfs, Cgit: *cgit, AuthMode: envDefault("CGIT_AUTH_MODE", "public"), BaseURL: strings.TrimRight(os.Getenv("CGIT_BASE_URL"), "/"), Username: os.Getenv("CGIT_USERNAME"), PasswordFile: os.Getenv("CGIT_PASSWORD_FILE")}
	if c.BaseURL != "" {
		u, e := url.Parse(c.BaseURL)
		if e != nil || !contains([]string{"http", "https"}, u.Scheme) || u.Host == "" || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || u.User != nil || strings.IndexFunc(c.BaseURL, unicode.IsSpace) >= 0 {
			return fmt.Errorf("CGIT_BASE_URL must be an http(s) origin")
		}
	}
	if !contains([]string{"public", "private", "access"}, c.AuthMode) {
		return fmt.Errorf("invalid CGIT_AUTH_MODE")
	}
	ar, gl := os.Getenv("CGIT_ACCESS_ROOT"), os.Getenv("CGIT_GITOLITE")
	if c.AuthMode == "access" {
		if c.Username != "" || c.PasswordFile != "" || !filepath.IsAbs(ar) || !isDir(ar) || !filepath.IsAbs(gl) || !executable(gl) {
			return fmt.Errorf("access mode requires valid access root/Gitolite, without single-account credentials")
		}
		c.Access = &AccessOptions{ar, gl}
	} else {
		if ar != "" || gl != "" || (c.Username == "") != (c.PasswordFile == "") || (c.Username != "" && !userRE.MatchString(c.Username)) {
			return fmt.Errorf("invalid single-account configuration")
		}
	}
	for _, kv := range []struct {
		k string
		p *bool
	}{{"CGIT_HTTP_PUSH", &c.HTTPPush}, {"CGIT_SSH_PUSH", &c.SSHPush}} {
		v := envDefault(kv.k, "0")
		if v != "0" && v != "1" {
			return fmt.Errorf("%s must be 0 or 1", kv.k)
		}
		*kv.p = v == "1"
	}
	if c.AuthMode != "access" && (c.AuthMode == "private" || c.HTTPPush) && c.PasswordFile == "" {
		return fmt.Errorf("private mode and push require credentials")
	}
	if c.PasswordFile != "" {
		b, e := os.ReadFile(c.PasswordFile)
		if e != nil {
			return e
		}
		b = []byte(strings.TrimRight(string(b), "\r\n"))
		if len(b) == 0 || strings.ContainsAny(string(b), "\r\n") {
			return fmt.Errorf("password must be one nonempty line")
		}
	}
	var e error
	c.MaxSize, e = strconv.ParseInt(envDefault("CGIT_LFS_MAX_SIZE", "10737418240"), 10, 64)
	if e != nil || c.MaxSize < 1 || c.MaxSize > 1<<40 {
		return fmt.Errorf("invalid CGIT_LFS_MAX_SIZE")
	}
	ssh := os.Getenv("CGIT_SSH_CLONE_URL")
	if strings.IndexFunc(ssh, unicode.IsSpace) >= 0 || (ssh != "" && !strings.Contains(ssh, "$CGIT_REPO_URL")) {
		return fmt.Errorf("invalid SSH clone URL")
	}
	b, e := os.ReadFile(*source)
	if e != nil {
		return e
	}
	ss, e := settings(string(b))
	if e != nil {
		return e
	}
	scans := 0
	var src strings.Builder
	for _, kv := range ss {
		k, v := kv[0], kv[1]
		if k == "about-filter" && v == "/usr/local/lib/cgit-personal/about_filter.py" {
			v = "/usr/local/bin/about-filter"
		}
		if k == "source-filter" && v == "/usr/local/lib/cgit-personal/source_filter.py" {
			v = "/usr/local/bin/source-filter"
		}
		if (k == "about-filter" || k == "source-filter") && strings.Contains(v, ".py") {
			return fmt.Errorf("Python filters are unsupported; configure a Go filter executable")
		}
		if k == "include" || strings.HasPrefix(k, "repo.") {
			return fmt.Errorf("include/repo.* unsupported")
		}
		if k == "scan-path" {
			scans++
			if v != *repos {
				return fmt.Errorf("scan-path must match repository root")
			}
		}
		if contains([]string{"project-list", "strict-export", "remove-suffix", "enable-git-config", "scan-hidden-path"}, k) {
			if scans > 0 {
				return fmt.Errorf("%s must precede scan-path", k)
			}
			switch k {
			case "project-list":
				if !filepath.IsAbs(v) || strings.Contains(v, "$") {
					return fmt.Errorf("invalid project-list")
				}
				if _, e = projectEntries(v); e != nil {
					return e
				}
				c.Policy.ProjectList = v
			case "strict-export":
				if v == "" || strings.ContainsAny(v, "/\\$\r\n") || v == "." || v == ".." {
					return fmt.Errorf("invalid strict-export")
				}
				c.Policy.StrictExport = v
			default:
				expected := "1"
				if k == "scan-hidden-path" {
					expected = "0"
				}
				if v != expected {
					return fmt.Errorf("invalid %s", k)
				}
			}
		}
		if k != "clone-url" && k != "cache-size" {
			fmt.Fprintf(&src, "%s=%s\n", k, v)
		}
	}
	if scans != 1 {
		return fmt.Errorf("exactly one scan-path required")
	}
	if _, e = scan(*repos); e != nil {
		return e
	}
	c.WriteRoot = os.Getenv("CGIT_WRITE_ROOT")
	if c.WriteRoot != "" && !filepath.IsAbs(c.WriteRoot) {
		return fmt.Errorf("CGIT_WRITE_ROOT must be absolute")
	}
	dir := envDefault("CGIT_RUNTIME_DIR", "/var/cache/cgit/runtime")
	if e = os.MkdirAll(dir, 0755); e != nil {
		return e
	}
	if e = os.MkdirAll(filepath.Join(filepath.Dir(dir), "uploads"), 0755); e != nil {
		return e
	}
	c.CgitConfig = filepath.Join(dir, "cgitrc")
	if e = os.MkdirAll(writeRoot(&c), 0700); e != nil {
		return e
	}
	base := c.BaseURL
	if base == "" {
		base = "http://$HTTP_HOST"
	}
	clone := base + "/$CGIT_REPO_URL"
	if ssh != "" {
		clone += " " + ssh
	}
	header := ""
	if c.AuthMode == "access" {
		hp := filepath.Join(dir, "access-header.html")
		if e = atomicWrite(hp, []byte("<nav aria-label=\"Account\"><a href=\"/_login\">登录 / Sign in</a></nav>\n"), 0644); e != nil {
			return e
		}
		header = "header=" + hp + "\n"
	}
	if e = atomicWrite(c.CgitConfig, []byte("cache-size=0\n"+header+"remove-suffix=1\nenable-git-config=1\nscan-hidden-path=0\nclone-url="+clone+"\n"+src.String()), 0644); e != nil {
		return e
	}
	limit := (c.MaxSize + 1023) / 1024
	if limit < 1024 {
		limit = 1024
	}
	if e = atomicWrite(filepath.Join(dir, "limits.conf"), []byte(fmt.Sprintf("server.max-request-size = %d\n", limit)), 0644); e != nil {
		return e
	}
	return atomicWrite(filepath.Join(dir, "settings.json"), append(jsonBytes(c), '\n'), 0644)
}
