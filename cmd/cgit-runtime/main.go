package main

import (
	"errors"
	"flag"
	"fmt"
	r "github.com/zopiya/cgit/internal/runtime"
	"os"
	"path/filepath"
)

func main() {
	if os.Getenv("PATH") == "" {
		os.Setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
	}
	args := os.Args[1:]
	sub := ""
	aliases := map[string]string{"gateway.cgi": "gateway", "cgit-doctor": "doctor", "cgit-ssh": "ssh", "cgit-configure": "configure", "cgit-access": "access", "cgit-jobs": "jobs", "about-filter": "about-filter", "source-filter": "source-filter", "access-log": "access-log"}
	if s, ok := aliases[filepath.Base(os.Args[0])]; ok {
		sub = s
	} else if len(args) > 0 {
		sub = args[0]
		args = args[1:]
	}
	var err error
	switch sub {
	case "gateway":
		err = r.Gateway()
	case "configure":
		err = r.Configure(args)
	case "access":
		err = r.AccessCLI(args)
	case "ssh":
		err = r.SSH(args)
	case "doctor":
		err = r.Doctor(args)
	case "jobs":
		err = r.Jobs(args)
	case "about-filter":
		err = r.AboutFilter(args)
	case "source-filter":
		err = r.SourceFilter(args)
	case "access-log":
		err = r.AccessLog()
	default:
		err = fmt.Errorf("usage: cgit-runtime {gateway|configure|access|ssh|doctor|jobs|about-filter|source-filter|access-log}")
	}
	if errors.Is(err, flag.ErrHelp) {
		return
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "cgit-runtime:", err)
		os.Exit(1)
	}
}
