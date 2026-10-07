//go:build darwin || linux

package maven

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPreparationDrainsGeneratorChildrenBeforeReturning(t *testing.T) {
	for _, cancelRun := range []bool{true, false} {
		t.Run(map[bool]string{true: "cancelled", false: "parent-exited"}[cancelRun], func(t *testing.T) {
			root := t.TempDir()
			marker := filepath.Join(root, "writes")
			script := filepath.Join(root, "plugin")
			quote := "'" + strings.ReplaceAll(marker, "'", "'\"'\"'") + "'"
			body := "#!/bin/sh\n(while :; do printf x >> " + quote + "; sleep 0.02; done) >/dev/null 2>&1 &\n"
			if cancelRun {
				body += "wait\n"
			} else {
				body += "sleep 0.1\nexit 0\n"
			}
			if err := os.WriteFile(script, []byte(body), 0700); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			command := exec.CommandContext(ctx, script)
			done := make(chan error, 1)
			go func() { done <- runOwnedCommand(command) }()
			deadline := time.Now().Add(5 * time.Second)
			for {
				if stat, e := os.Stat(marker); e == nil && stat.Size() > 0 {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("generator did not start")
				}
				time.Sleep(10 * time.Millisecond)
			}
			if cancelRun {
				cancel()
			}
			select {
			case err := <-done:
				if cancelRun && err == nil {
					t.Fatal("cancelled command succeeded")
				}
				if !cancelRun && err != nil {
					t.Fatal(err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("child process drain stalled")
			}
			live, err := processGroupLive(command.Process.Pid)
			if err != nil || live {
				t.Fatalf("generator group remains active: %v %v", live, err)
			}
			before, err := os.ReadFile(marker)
			if err != nil {
				t.Fatal(err)
			}
			time.Sleep(60 * time.Millisecond)
			after, err := os.ReadFile(marker)
			if err != nil {
				t.Fatal(err)
			}
			if string(before) != string(after) {
				t.Fatal("generator wrote after preparation returned")
			}
		})
	}
}
