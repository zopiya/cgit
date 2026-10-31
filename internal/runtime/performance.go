package runtime

import (
	"fmt"
	"strconv"
	"syscall"
)

// Performance limits are administrator inputs, never derived from request headers.
func envInt64(name string, fallback, minimum, maximum int64) (int64, error) {
	s := envDefault(name, strconv.FormatInt(fallback, 10))
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil || n < minimum || n > maximum {
		return 0, fmt.Errorf("%s must be an integer between %d and %d", name, minimum, maximum)
	}
	return n, nil
}

func configurePerformance(c *Config) (string, error) {
	var err error
	c.MaxRequestSize, err = envInt64("CGIT_HTTP_MAX_REQUEST_SIZE", max(c.MaxSize, 1<<20), max(c.MaxSize, 1<<20), 1<<40)
	if err != nil {
		return "", err
	}
	idle, err := envInt64("CGIT_HTTP_IDLE_TIMEOUT", 300, 1, 86400)
	if err != nil {
		return "", err
	}
	connections, err := envInt64("CGIT_HTTP_MAX_CONNECTIONS", 1024, 16, 65535)
	if err != nil {
		return "", err
	}
	c.LFSMinFreeBytes, err = envInt64("CGIT_LFS_MIN_FREE_BYTES", 0, 0, 1<<50)
	if err != nil {
		return "", err
	}
	threads, err := envInt64("CGIT_GIT_PACK_THREADS", 0, 0, 256)
	if err != nil {
		return "", err
	}
	c.PackThreads = int(threads)
	c.PackWindowMemory, err = envInt64("CGIT_GIT_PACK_WINDOW_MEMORY", 0, 0, 1<<40)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("server.max-request-size = %d\nserver.max-read-idle = %d\nserver.max-write-idle = %d\nserver.max-connections = %d\n", (c.MaxRequestSize+1023)/1024, idle, idle, connections), nil
}

// Each CGI/SSH child gets explicit administrator-controlled Git tuning. Do not
// forward a client's GIT_CONFIG_* environment into these trusted parameters.
func (c *Config) transportEnv(env []string) []string {
	if c.PackThreads == 0 && c.PackWindowMemory == 0 {
		return env
	}
	pairs := [][2]string{}
	if c.PackThreads > 0 {
		pairs = append(pairs, [2]string{"pack.threads", strconv.Itoa(c.PackThreads)})
	}
	if c.PackWindowMemory > 0 {
		pairs = append(pairs, [2]string{"pack.windowMemory", strconv.FormatInt(c.PackWindowMemory, 10)})
	}
	env = setEnv(env, "GIT_CONFIG_COUNT", strconv.Itoa(len(pairs)))
	for i, p := range pairs {
		env = setEnv(env, fmt.Sprintf("GIT_CONFIG_KEY_%d", i), p[0])
		env = setEnv(env, fmt.Sprintf("GIT_CONFIG_VALUE_%d", i), p[1])
	}
	return env
}

// Admission control, not a reservation or quota: concurrent/external writes
// can consume space after this observation. The filesystem remains authoritative.
func (c *Config) checkUploadSpace(directory string, size int64) error {
	if c.LFSMinFreeBytes == 0 {
		return nil
	}
	var st syscall.Statfs_t
	if err := syscall.Statfs(directory, &st); err != nil {
		return err
	}
	if st.Bsize <= 0 {
		return fmt.Errorf("filesystem block size unavailable")
	}
	required := uint64(size) + uint64(c.LFSMinFreeBytes)
	blockSize := uint64(st.Bsize)
	blocks := required / blockSize
	if required%blockSize != 0 {
		blocks++
	}
	if uint64(st.Bavail) < blocks {
		return fail(507, "Insufficient storage for upload and configured free-space reserve")
	}
	return nil
}
