#!/usr/bin/env python3
"""Run a command on a terminal of its own and hang that terminal up (tests/test_hangup_stop.sh).

    hangup.py --ready FILE [--out FILE] [--err FILE] [--nohup] [--how close|group]
              [--start-wait SECONDS] [--wait SECONDS] [--transcript FILE] [--pid FILE]
              -- COMMAND [ARG ...]

COMMAND starts as the session leader of a new pseudo-terminal, as the command of a tmux pane
or of a terminal window does; that terminal is its controlling terminal and its stdin, and its
stdout and stderr unless --out/--err send them to a file (output written to the terminal is
kept in --transcript). Once FILE exists, the terminal hangs up:

  close  the terminal closes (its master side, as tmux does for a pane of a killed session
         or a terminal emulator for a closed window): the kernel sends SIGHUP to the session
         leader, and every later write to the terminal fails
  group  SIGHUP to the command's process group, as an interactive shell sends it to its jobs
         when its own terminal goes, and the kernel to the foreground group once the session
         leader has exited; the terminal stays readable

Prints "rc=<exit code> seconds=<from the hang-up to the exit>" (128+N for a death by signal N)
and exits with that code; 97 when FILE never appeared within --start-wait (default 30 s), 98
when COMMAND outlived --wait (default 60 s) after the hang-up (it is still running then, and
the caller's teardown stops it). --nohup starts COMMAND with SIGHUP ignored, as nohup does;
otherwise SIGHUP and SIGINT start at their defaults, whatever the test runner left. --pid
writes COMMAND's process ID to FILE.
"""
import argparse
import os
import pty
import select
import signal
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready', required=True)
    parser.add_argument('--out'); parser.add_argument('--err'); parser.add_argument('--transcript'); parser.add_argument('--pid')
    parser.add_argument('--nohup', action='store_true')
    parser.add_argument('--how', choices=('close', 'group'), default='close')
    parser.add_argument('--start-wait', type=float, default=30)
    parser.add_argument('--wait', type=float, default=60)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    pid, master = pty.fork()
    if pid == 0:
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN if args.nohup else signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            for path, fd in ((args.out, 1), (args.err, 2)):
                if path:
                    target = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    os.dup2(target, fd); os.close(target)
            os.execvp(command[0], command)
        finally:
            os._exit(127)
    transcript = open(args.transcript or os.devnull, 'ab')
    if args.pid:
        with open(args.pid, 'w') as out: out.write(str(pid))

    def drain(timeout):
        nonlocal master
        if master is None:
            time.sleep(timeout); return
        ready, _, _ = select.select([master], [], [], timeout)
        if ready:
            try: data = os.read(master, 65536)
            except OSError: data = b''
            if data: transcript.write(data); transcript.flush()
            else: os.close(master); master = None

    def exited():
        done, status = os.waitpid(pid, os.WNOHANG)
        if not done: return None
        return 128 + os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)

    deadline = time.monotonic() + args.start_wait
    while not os.path.exists(args.ready):
        code = exited()
        if code is not None:
            print('rc=%d seconds=-1 (ended before %s appeared)' % (code, args.ready))
            return 97
        if time.monotonic() > deadline:
            print('rc=97 seconds=-1 (%s never appeared)' % args.ready)
            return 97
        drain(0.05)
    if args.how == 'close':
        if master is not None: os.close(master); master = None
    else:
        os.killpg(pid, signal.SIGHUP)
    hung_up = time.monotonic()
    while True:
        code = exited()
        if code is not None: break
        if time.monotonic() - hung_up > args.wait:
            print('rc=98 seconds=%d (still running)' % args.wait)
            return 98
        drain(0.05)
    print('rc=%d seconds=%d' % (code, time.monotonic() - hung_up))
    return code


if __name__ == '__main__':
    sys.exit(main())
