package runtime

import (
	"fmt"
	"github.com/mattn/go-shellwords"
	"os"
	"strings"
)

func SSH(args []string) error {
	if len(args) != 1 || !userRE.MatchString(args[0]) {
		return fmt.Errorf("invalid forced command identity")
	}
	user := args[0]
	original := os.Getenv("SSH_ORIGINAL_COMMAND")
	if original == "" || len(original) > 4096 || strings.ContainsAny(original, "\r\n\x00") {
		return fmt.Errorf("interactive shell is disabled")
	}
	parser := shellwords.NewParser()
	parser.ParseEnv = false
	parser.ParseBacktick = false
	words, e := parser.Parse(original)
	if e != nil || len(words) < 2 {
		return fmt.Errorf("malformed SSH command")
	}
	action := words[0]
	if !contains([]string{"git-upload-pack", "git-receive-pack", "git-lfs-authenticate"}, action) {
		return fmt.Errorf("unsupported SSH command")
	}
	c, e := LoadConfig()
	if e != nil {
		return e
	}
	if c.AuthMode != "access" {
		return fmt.Errorf("SSH requires access mode")
	}
	a, e := NewAccess(c)
	if e != nil {
		return e
	}
	defer a.Close()
	repo, name, e := repository(c, strings.TrimPrefix(words[1], "/"), true)
	if e != nil {
		return e
	}
	if action == "git-lfs-authenticate" {
		if len(words) != 3 || !contains([]string{"upload", "download"}, words[2]) || c.BaseURL == "" {
			return fmt.Errorf("invalid LFS authentication request or missing base URL")
		}
		op := "R"
		if words[2] == "upload" {
			op = "W"
		}
		if e = a.Require(name, user, op); e != nil {
			return e
		}
		token, e := issueToken(c, user, strings.TrimSuffix(name, ".git"), words[2], a.State.Generation)
		if e != nil {
			return e
		}
		printJSON(map[string]any{"href": c.BaseURL + "/" + escapePath(strings.TrimSuffix(name, ".git")) + ".git/info/lfs", "header": map[string]string{"Authorization": "Bearer " + token}, "expires_in": 300})
		return nil
	}
	if len(words) != 2 {
		return fmt.Errorf("unsupported Git command")
	}
	op := "R"
	if action == "git-receive-pack" {
		if !c.SSHPush {
			return fmt.Errorf("SSH writes are disabled")
		}
		op = "W"
	}
	if e = a.Require(name, user, op); e != nil {
		return e
	}
	env, e := a.GitEnv(name, user)
	if e != nil {
		return e
	}
	if p := os.Getenv("GIT_PROTOCOL"); contains([]string{"version=0", "version=1", "version=2"}, p) {
		env = append(env, "GIT_PROTOCOL="+p)
	}
	g := &gateway{access: a}
	g.inheritLocks()
	return execProgram(action, []string{repo}, env)
}
