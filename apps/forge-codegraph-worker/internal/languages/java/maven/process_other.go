//go:build !darwin && !linux

package maven

import (
	"fmt"
	"os/exec"
)

func runOwnedCommand(*exec.Cmd) error {
	return fmt.Errorf("Maven preparation requires process-group containment on Linux or macOS")
}
