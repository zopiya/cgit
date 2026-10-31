package runtime

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type DoctorReport struct {
	OK           bool            `json:"ok"`
	Repositories []string        `json:"repositories"`
	Findings     []string        `json:"findings"`
	Checks       map[string]bool `json:"checks"`
}

func Check(ctx context.Context, c *Config, checkGit, checkLFS bool) DoctorReport {
	r := DoctorReport{Repositories: []string{}, Findings: []string{}, Checks: map[string]bool{"git": checkGit, "lfs": checkLFS}}
	add := func(e any) { r.Findings = append(r.Findings, fmt.Sprint(e)) }
	if !isDir(c.LFS) {
		add("lfs: directory unavailable")
	}
	if c.AuthMode == "access" {
		a, e := NewAccess(c)
		if e != nil {
			add(e)
		} else {
			a.Close()
			for _, p := range []string{"ssh/authorized_keys", "lfs-token.key"} {
				if !regular(filepath.Join(c.Access.Root, p)) {
					add("access: missing " + p)
				}
			}
		}
	} else if c.PasswordFile != "" {
		if _, e := os.ReadFile(c.PasswordFile); e != nil {
			add("password file unreadable")
		}
	}
	names, e := scan(c.Repos)
	if e != nil {
		add(e)
	}
	for _, name := range names {
		repo, canonical, e := repository(c, name, false)
		if e != nil {
			add(e)
			continue
		}
		r.Repositories = append(r.Repositories, canonical)
		run := func(args ...string) ([]byte, error) {
			return command(ctx, cleanEnv(""), nil, "git", append([]string{"-c", "safe.directory=" + repo, "--git-dir", repo}, args...)...)
		}
		if checkGit {
			if _, e = run("fsck", "--full"); e != nil {
				add(name + ": " + e.Error())
			}
		}
		if !checkLFS {
			continue
		}
		listed, e := run("rev-list", "--objects", "--all")
		if e != nil {
			add(e)
			continue
		}
		var objects strings.Builder
		for _, line := range strings.Split(strings.TrimSpace(string(listed)), "\n") {
			if line != "" {
				objects.WriteString(strings.SplitN(line, " ", 2)[0] + "\n")
			}
		}
		args := []string{"-c", "safe.directory=" + repo, "--git-dir", repo, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"}
		out, e := command(ctx, cleanEnv(""), strings.NewReader(objects.String()), "git", args...)
		if e != nil {
			add(e)
			continue
		}
		for _, line := range bytes.Split(out, []byte("\n")) {
			fields := strings.Fields(string(line))
			if len(fields) != 3 || fields[1] != "blob" {
				continue
			}
			n, e := strconv.ParseInt(fields[2], 10, 64)
			if e != nil || n > 1024 {
				continue
			}
			b, e := run("cat-file", "blob", fields[0])
			if e != nil {
				add(e)
				continue
			}
			oid, size, ok := parsePointer(b)
			if !ok {
				continue
			}
			p, e := ObjectPath(c, canonical, oid)
			if e != nil {
				add(e)
				continue
			}
			st, e := os.Stat(p)
			if e != nil || st.Size() != size {
				add(name + ": missing or wrong size LFS object " + oid)
			}
		}
		root := filepath.Join(c.LFS, canonical, "objects")
		if !exists(root) {
			continue
		}
		e = filepath.WalkDir(root, func(p string, d os.DirEntry, e error) error {
			if e != nil {
				return e
			}
			if d.Type()&os.ModeSymlink != 0 {
				return fmt.Errorf("unsafe LFS symlink: %s", p)
			}
			if d.IsDir() {
				return nil
			}
			if strings.HasPrefix(d.Name(), ".upload-") {
				add(name + ": interrupted upload " + d.Name())
				return nil
			}
			expected, e := ObjectPath(c, canonical, d.Name())
			if e != nil {
				add(name + ": invalid LFS object path " + d.Name())
				return nil
			}
			h, e := digest(p)
			if e != nil {
				return e
			}
			canonicalPath, pathErr := filepath.EvalSymlinks(p)
			if pathErr != nil {
				return pathErr
			}
			if expected != canonicalPath || h != d.Name() {
				add(name + ": corrupt or misplaced LFS object " + d.Name())
			}
			return nil
		})
		if e != nil {
			add(e)
		}
	}
	r.OK = len(r.Findings) == 0
	return r
}
func Doctor(args []string) error {
	f := flag.NewFlagSet("doctor", flag.ContinueOnError)
	g := f.Bool("git", false, "")
	l := f.Bool("lfs", false, "")
	if e := f.Parse(args); e != nil {
		return e
	}
	c, e := LoadConfig()
	if e != nil {
		printJSON(map[string]any{"ok": false, "findings": []string{e.Error()}})
		return e
	}
	r := Check(context.Background(), c, *g, *l)
	printJSON(r)
	if !r.OK {
		return fmt.Errorf("integrity check failed")
	}
	return nil
}
