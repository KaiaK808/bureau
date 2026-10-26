#!/usr/bin/env python3
"""Provider activity deadlines and identity-checked ownership of tool processes.

No provider/Linear calls, no extra output reader. Process arguments and inherited
ownership markers are transient; none belongs in the metadata evidence store.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import threading
import time
import uuid

OWNER_ENV = 'BUREAU_ACTIVITY_OWNER'


class Activity:
    def __init__(self, runner, seconds, child_seconds, clock=time.monotonic):
        self.runner, self.seconds, self.child_seconds = runner, seconds, child_seconds
        self.clock = clock
        self.last = clock()
        self.tools = {}
        self.seen = {}
        self.lock = threading.Lock()

    def line(self, line):
        try: event = json.loads(line)
        except (ValueError, UnicodeError): return
        if not isinstance(event, dict): return
        with self.lock:
            useful = False
            kind = event.get('type')
            if self.runner == 'codex':
                item = event.get('item')
                if kind in ('item.started', 'item.updated', 'item.completed') and isinstance(item, dict):
                    key, typ = item.get('id'), item.get('type')
                    if isinstance(key, str) and key and typ in (
                            'agent_message', 'reasoning', 'command_execution', 'file_change', 'mcp_tool_call', 'web_search'):
                        # Repeated lifecycle packets and unchanged output are pings,
                        # not progress. Terminal packets close a tool only once.
                        observed = {k:item[k] for k in ('id', 'type', 'text', 'command', 'aggregated_output',
                                    'changes', 'status', 'exit_code', 'result', 'error') if k in item}
                        signature = hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).digest()
                        slot = (kind, key)
                        if self.seen.get(slot) == signature: return
                        self.seen[slot] = signature; useful = True
                        if typ == 'command_execution':
                            command = item.get('command')
                            if kind == 'item.started' and isinstance(command, str) and command:
                                self.tools[key] = command
                            elif kind == 'item.completed': self.tools.pop(key, None)
                elif kind == 'turn.completed' and isinstance(event.get('usage'), dict):
                    if kind not in self.seen:
                        self.seen[kind] = True; useful = True
            else:
                # Qualified Claude stream-json: complete assistant/user messages,
                # and incremental content deltas from --include-partial-messages.
                if kind in ('assistant', 'user'):
                    message = event.get('message')
                    content = message.get('content') if isinstance(message, dict) else None
                    if isinstance(content, list):
                        signature = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).digest()
                        slot = (kind, message.get('id') if isinstance(message.get('id'), str) else None)
                        if self.seen.get(slot) == signature: return
                        self.seen[slot] = signature
                        for block in content:
                            if not isinstance(block, dict): continue
                            typ = block.get('type')
                            if typ in ('text', 'thinking') and isinstance(block.get(typ), str) and block[typ].strip():
                                useful = True
                            elif typ == 'tool_use' and isinstance(block.get('id'), str):
                                useful = True
                                inputs = block.get('input')
                                if block.get('name') == 'Bash' and isinstance(inputs, dict) and isinstance(inputs.get('command'), str):
                                    self.tools[block['id']] = inputs['command']
                            elif typ == 'tool_result' and isinstance(block.get('tool_use_id'), str):
                                useful = True; self.tools.pop(block['tool_use_id'], None)
                elif kind == 'stream_event':
                    inner = event.get('event')
                    delta = inner.get('delta') if isinstance(inner, dict) and inner.get('type') == 'content_block_delta' else None
                    if isinstance(delta, dict):
                        typ = delta.get('type')
                        field = {'text_delta':'text', 'thinking_delta':'thinking', 'input_json_delta':'partial_json'}.get(typ) if isinstance(typ, str) else None
                        useful = bool(field and isinstance(delta.get(field), str) and delta[field].strip())
                elif kind == 'result' and isinstance(event.get('result'), str) and kind not in self.seen:
                    self.seen[kind] = True; useful = True
            if useful: self.last = self.clock()

    def expired(self, processes):
        with self.lock:
            # A provider, wrapper or unfinished item cannot borrow the tool
            # allowance. A live owned process must match an active tool command.
            extended = any(processes.tool_alive(command) for command in self.tools.values())
            window = self.child_seconds if extended else self.seconds
            return self.seconds > 0 and self.clock() - self.last >= window


def command_matches(command, argv):
    try:
        wanted = shlex.split(command)
        if not wanted or not argv: return False
        # Provider events can themselves contain an absolute shell wrapper.
        if len(wanted) == 3 and os.path.basename(wanted[0]) in ('bash', 'sh', 'zsh') and wanted[1] in ('-c', '-lc'):
            command = wanted[2]; wanted = shlex.split(command)
        for index, arg in enumerate(argv[:-1]):
            if arg in ('-c', '-lc') and os.path.basename(argv[0]) in ('bash', 'sh', 'zsh'):
                return argv[index + 1] == command
        executable = (os.path.realpath(argv[0]) == os.path.realpath(wanted[0]) if os.path.isabs(wanted[0])
                      else os.path.basename(argv[0]) == wanted[0])
        # Framework Python on macOS rewrites argv[0] from the bin/python3
        # launcher to Python.app/.../Python. An exact script/-c/-m + argv
        # match still identifies this tool, not the provider's Python CLI.
        if (not executable and sys.platform == 'darwin' and len(wanted) > 1
                and (os.path.isabs(wanted[1]) or wanted[1] in ('-c', '-m')) and argv[1:] == wanted[1:]
                and all(re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', os.path.basename(x).lower()) for x in (wanted[0], argv[0]))):
            executable = True
        return executable and argv[1:] == wanted[1:]
    except ValueError: return False


class _BsdInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid', 'nice')]
    _fields_ += [('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]


class ProcessTable:
    """Native local inspection, without ps, shell commands or PID-only kills."""
    def __init__(self):
        if sys.platform == 'darwin':
            self.libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            self.libc = ctypes.CDLL(None, use_errno=True)
        elif not Path('/proc/self/stat').is_file():
            raise OSError('provider process inspection requires Linux procfs or macOS libproc')

    def snapshot(self):
        if sys.platform == 'darwin':
            needed = self.libproc.proc_listpids(1, 0, None, 0)
            if needed <= 0: raise OSError('provider process listing unavailable')
            pids = (ctypes.c_int * (needed // 4 + 1024))()
            size = self.libproc.proc_listpids(1, 0, pids, ctypes.sizeof(pids))
            if size <= 0: raise OSError('provider process listing unavailable')
            result = {}
            for pid in pids[:size // 4]:
                info = _BsdInfo()
                if self.libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) == ctypes.sizeof(info) and info.uid == os.getuid():
                    result[pid] = (info.ppid, (info.start_sec, info.start_usec), info.status == 5)
            return result
        result = {}
        for path in Path('/proc').iterdir():
            if not path.name.isdigit(): continue
            try:
                if path.stat().st_uid != os.getuid(): continue
                fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
                result[int(path.name)] = (int(fields[1]), fields[19], fields[0] == 'Z')
            except (OSError, ValueError, IndexError): pass  # raced an exit
        return result

    def args_env(self, pid):
        try:
            if sys.platform != 'darwin':
                argv = (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
                env = (Path('/proc')/str(pid)/'environ').read_bytes().split(b'\0')
                return [x.decode(errors='replace') for x in argv if x], env
            mib = (ctypes.c_int * 3)(1, 49, pid)  # KERN_PROCARGS2
            buf = ctypes.create_string_buffer(1024 * 1024)
            size = ctypes.c_size_t(len(buf))
            if self.libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0: return [], []
            argc = ctypes.c_int.from_buffer_copy(buf.raw[:4]).value
            data = buf.raw[4:size.value].split(b'\0', 1)[1].lstrip(b'\0').split(b'\0')
            return [x.decode(errors='replace') for x in data[:argc]], data[argc:]
        except (OSError, ValueError, IndexError): return [], []


class OwnedProcesses:
    """Explicit per-call inherited marker + observed ancestry + birth identity.

    The marker finds double-forked/setsid descendants after reparenting. An
    observed descendant remains owned after clearing its environment. A reused
    PID never inherits ownership. Linux adopts orphans to reap them directly;
    macOS's launchd reaps detached orphans, which we wait to disappear.
    """
    def __init__(self):
        self.table = ProcessTable()
        self.token = uuid.uuid4().hex
        self.marker = (OWNER_ENV + '=' + self.token).encode()
        self.owned = {}; self.current = {}; self.root = None
        # Processes predating this random marker cannot carry it. Cache birth
        # identities, never PIDs alone, to avoid rereading every user's process
        # environment on each tick of a long provider call.
        self.unowned = {pid:row[1] for pid,row in self.table.snapshot().items()}
        if sys.platform.startswith('linux'):
            # PR_SET_CHILD_SUBREAPER; only this adapter's descendants are adopted.
            if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
                raise OSError('cannot adopt owned provider descendants')

    def attach(self, pid):
        self.root = pid
        self.scan()

    def scan(self):
        rows = self.table.snapshot()
        owned = {pid: row for pid, row in rows.items() if self.owned.get(pid) == row[1]}
        if self.root in rows and self.root not in self.owned:
            owned[self.root] = rows[self.root]
        for pid, row in rows.items():
            if pid in owned or row[2] or self.unowned.get(pid) == row[1]: continue
            _, env = self.table.args_env(pid)
            if self.marker in env: owned[pid] = row
            elif env: self.unowned[pid] = row[1]
        changed = True
        while changed:
            changed = False
            for pid, row in rows.items():
                if pid not in owned and row[0] in owned:
                    owned[pid] = row; changed = True
        for pid, row in owned.items(): self.owned[pid] = row[1]
        self.current = owned

    def tool_alive(self, command):
        return any(pid != self.root and not row[2] and command_matches(command, self.table.args_env(pid)[0])
                   for pid, row in self.current.items())

    def signal(self, signum):
        rows = self.table.snapshot()
        # Descendants first, while their original parent can still reap them.
        for pid in sorted(self.current, key=lambda pid: pid == self.root):
            if pid in rows and self.owned.get(pid) == rows[pid][1] and not rows[pid][2]:
                try: os.kill(pid, signum)
                except ProcessLookupError: pass
                except PermissionError:
                    # macOS can deny signalling a just-exited/reparented PID.
                    # Recheck birth/liveness; a live owned denial stays an error.
                    fresh = self.table.snapshot().get(pid)
                    if fresh and fresh[1] == self.owned[pid] and not fresh[2]: raise

    def drain(self):
        started = time.monotonic()
        while True:
            self.scan()
            for pid in list(self.current):
                if pid == self.root: continue  # Popen owns this wait status
                try: os.waitpid(pid, os.WNOHANG)
                except ChildProcessError: pass
            self.scan()
            remaining = {pid: row for pid, row in self.current.items() if pid != self.root}
            if not remaining: return
            self.signal(signal.SIGKILL if time.monotonic() - started >= .3 else signal.SIGTERM)
            # Never release an execution lease while a known writer (or an
            # unreaped owned orphan) survives. Kernel-delayed teardown keeps
            # this invocation draining rather than reporting a false stop.
            time.sleep(.05)
