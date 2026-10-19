"""
A tied program's watcher (``forge_common.processes``), run as a file by the
starter's Python: ``python -I -S _watcher.py <fd> <tag> <program> [args...]``.

It forks its watching half off, then execs the program in its own process.
The watching half reads the pipe on ``<fd>`` until it ends (its starter
closed it, or went), then kills its process group. Standard library only,
and nothing else imported: it runs before every command.
"""

import os
import signal
import sys


def main() -> None:
    fd = int(sys.argv[1])
    program = sys.argv[3:]
    if os.fork() == 0:
        null = os.open(os.devnull, os.O_RDWR)
        for target in (0, 1, 2):
            os.dup2(null, target)
        try:
            while os.read(fd, 64):
                pass
        except OSError:
            pass
        os.killpg(0, signal.SIGKILL)
        os._exit(0)
    os.close(fd)
    # Python ignores these; the program expects them as they were.
    for name in ("SIGPIPE", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    try:
        os.execv(program[0], program)
    except OSError as error:
        os.write(2, f"{program[0]}: {error.strerror or error}\n".encode())
        os._exit(127 if isinstance(error, FileNotFoundError) else 126)


if __name__ == "__main__":
    main()
