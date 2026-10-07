//go:build unix

package github

import (
	"errors"
	"os"
	"os/exec"
	"syscall"
)

// Git spawns transport helpers. Cancel the process group so a download cannot
// continue writing into the checkout after the parent Git process is killed.
func configureCancellation(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if errors.Is(err, syscall.ESRCH) {
			return os.ErrProcessDone
		}
		return err
	}
}
