package runtime

import (
	"context"
	"errors"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPerformanceLimitsAndTransport(t *testing.T) {
	names := []string{"CGIT_HTTP_MAX_REQUEST_SIZE", "CGIT_HTTP_IDLE_TIMEOUT", "CGIT_HTTP_MAX_CONNECTIONS", "CGIT_LFS_MIN_FREE_BYTES", "CGIT_GIT_PACK_THREADS", "CGIT_GIT_PACK_WINDOW_MEMORY"}
	for _, name := range names {
		t.Setenv(name, "")
	}
	c := &Config{MaxSize: 100 << 30}
	limits, err := configurePerformance(c)
	if err != nil || c.MaxRequestSize != 100<<30 || !strings.Contains(limits, "server.max-read-idle = 300") {
		t.Fatal(limits, err)
	}
	t.Setenv("CGIT_HTTP_MAX_REQUEST_SIZE", "214748364800")
	t.Setenv("CGIT_GIT_PACK_THREADS", "2")
	t.Setenv("CGIT_GIT_PACK_WINDOW_MEMORY", "67108864")
	limits, err = configurePerformance(c)
	if err != nil || !strings.Contains(limits, "server.max-request-size = 209715200") {
		t.Fatal(limits, err)
	}
	for key, want := range map[string]string{"pack.threads": "2", "pack.windowMemory": "67108864"} {
		out, err := command(context.Background(), c.transportEnv(cleanEnv("")), nil, "git", "config", "--get", key)
		if err != nil || strings.TrimSpace(string(out)) != want {
			t.Fatal(key, string(out), err)
		}
	}
	for _, kv := range [][2]string{{"CGIT_HTTP_MAX_REQUEST_SIZE", "1024"}, {"CGIT_HTTP_IDLE_TIMEOUT", "0"}, {"CGIT_HTTP_MAX_CONNECTIONS", "-1"}, {"CGIT_LFS_MIN_FREE_BYTES", "-1"}, {"CGIT_GIT_PACK_THREADS", "257"}, {"CGIT_GIT_PACK_WINDOW_MEMORY", "NaN"}} {
		t.Run(kv[0], func(t *testing.T) {
			t.Setenv(kv[0], kv[1])
			if _, err := configurePerformance(&Config{MaxSize: 100 << 30}); err == nil {
				t.Fatal("invalid limit accepted")
			}
		})
	}
}

func TestUploadStorageAdmission(t *testing.T) {
	dir := t.TempDir()
	c := &Config{LFSMinFreeBytes: 1}
	if err := c.checkUploadSpace(dir, 0); err != nil {
		t.Fatal(err)
	}
	err := c.checkUploadSpace(dir, math.MaxInt64-1)
	var response *Error
	if !errors.As(err, &response) || response.Status != 507 {
		t.Fatal("oversized upload must fail before writing", err)
	}
	c.LFSMinFreeBytes = 0
	if err := c.checkUploadSpace(filepath.Join(dir, "missing"), 0); err != nil {
		t.Fatal("disabled admission", err)
	}
	c.LFSMinFreeBytes = 1
	if err := c.checkUploadSpace(filepath.Join(dir, "missing"), 0); err == nil {
		t.Fatal("filesystem errors must fail closed")
	}
}

func TestConfiguredLimitsFile(t *testing.T) {
	root := t.TempDir()
	repos := filepath.Join(root, "repos")
	os.Mkdir(repos, 0700)
	source := filepath.Join(root, "cgitrc")
	write(t, source, "scan-path="+repos+"\n")
	for _, name := range []string{"CGIT_AUTH_MODE", "CGIT_USERNAME", "CGIT_PASSWORD_FILE", "CGIT_ACCESS_ROOT", "CGIT_GITOLITE", "CGIT_HTTP_PUSH", "CGIT_SSH_PUSH", "CGIT_BASE_URL", "CGIT_SSH_CLONE_URL"} {
		t.Setenv(name, "")
	}
	t.Setenv("CGIT_RUNTIME_DIR", filepath.Join(root, "runtime"))
	t.Setenv("CGIT_LFS_MAX_SIZE", "107374182400")
	t.Setenv("CGIT_HTTP_MAX_REQUEST_SIZE", "214748364800")
	t.Setenv("CGIT_HTTP_IDLE_TIMEOUT", "900")
	t.Setenv("CGIT_HTTP_MAX_CONNECTIONS", "512")
	if err := Configure([]string{"--source", source, "--repos", repos}); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(filepath.Join(root, "runtime/limits.conf"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"server.max-request-size = 209715200", "server.max-read-idle = 900", "server.max-write-idle = 900", "server.max-connections = 512"} {
		if !strings.Contains(string(b), want) {
			t.Fatal(want, string(b))
		}
	}
}
