//go:build !unix

package github

import "os/exec"

// Other platforms use exec.CommandContext's direct-process cancellation.
func configureCancellation(cmd *exec.Cmd) {}
