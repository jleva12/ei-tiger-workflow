//go:build darwin || linux

package maven

import (
	"errors"
	"log/slog"
	"os"
	"os/exec"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

// Maven plugins inherit a private process group. Cancellation kills the whole
// group, including forked compilers and generators. We drain it before returning
// because the caller may immediately remove the owned checkout and scratch.
func runOwnedCommand(command *exec.Cmd) error {
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error {
		err := unix.Kill(-command.Process.Pid, unix.SIGKILL)
		if errors.Is(err, unix.ESRCH) {
			return os.ErrProcessDone
		}
		return err
	}
	command.WaitDelay = 5 * time.Second
	if err := command.Start(); err != nil {
		return err
	}
	err := command.Wait()
	group := command.Process.Pid
	// Even a successful plugin must not leave background writers behind.
	_ = unix.Kill(-group, unix.SIGKILL)
	lastLog := time.Time{}
	for {
		live, inspectErr := processGroupLive(group)
		if inspectErr == nil && !live {
			return err
		}
		if inspectErr != nil && time.Since(lastLog) >= time.Minute {
			slog.Error("waiting for Maven child process drain before cleanup", "error", inspectErr, "process_group", group)
			lastLog = time.Now()
		}
		// Deliberately do not honor cancellation here: it must never cause a
		// checkout cleanup race with a still-running generator. Zombie entries
		// are excluded by processGroupLive because they cannot perform writes.
		_ = unix.Kill(-group, unix.SIGKILL)
		time.Sleep(20 * time.Millisecond)
	}
}
