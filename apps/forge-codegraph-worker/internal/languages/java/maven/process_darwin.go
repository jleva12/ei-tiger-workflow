package maven

import "golang.org/x/sys/unix"

func processGroupLive(group int) (bool, error) {
	entries, err := unix.SysctlKinfoProcSlice("kern.proc.pgrp", group)
	if err != nil {
		return false, err
	}
	for _, entry := range entries {
		// Darwin SZOMB is 5; a zombie has no userspace execution or open files.
		if int(entry.Eproc.Pgid) == group && entry.Proc.P_stat != 5 {
			return true, nil
		}
	}
	return false, nil
}
