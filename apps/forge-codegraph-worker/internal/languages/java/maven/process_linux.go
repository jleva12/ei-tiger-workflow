package maven

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

func processGroupLive(group int) (bool, error) {
	dir, err := os.Open("/proc")
	if err != nil {
		return false, err
	}
	defer dir.Close()
	for {
		entries, err := dir.ReadDir(256)
		for _, entry := range entries {
			if _, e := strconv.Atoi(entry.Name()); e != nil {
				continue
			}
			body, e := os.ReadFile("/proc/" + entry.Name() + "/stat")
			if errors.Is(e, os.ErrNotExist) {
				continue
			}
			if e != nil {
				return false, e
			}
			// The command name in parentheses can contain spaces and ')'.
			end := strings.LastIndexByte(string(body), ')')
			if end < 0 {
				return false, fmt.Errorf("invalid process stat")
			}
			fields := strings.Fields(string(body[end+1:]))
			if len(fields) < 3 {
				return false, fmt.Errorf("invalid process stat")
			}
			pgid, e := strconv.Atoi(fields[2])
			if e != nil {
				return false, e
			}
			if pgid == group && fields[0] != "Z" && fields[0] != "X" {
				return true, nil
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				return false, nil
			}
			return false, err
		}
	}
}
