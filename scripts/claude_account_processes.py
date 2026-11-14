#!/usr/bin/env python3
"""Inspect and stop current-user processes attached to profile login locks."""

import os
import signal
import subprocess
import sys
from pathlib import Path


def process_table():
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,uid=,tty=,lstart=,comm="],
        check=True,
        capture_output=True,
        text=True,
    )
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 9)
        if len(fields) == 10 and int(fields[2]) == os.getuid():
            pid, parent = map(int, fields[:2])
            processes[pid] = (parent, fields[3], " ".join(fields[4:9]), fields[9])
    return processes


def lock_holders(path, runner_code):
    result = subprocess.run(
        ["lsof", "-nP", "-a", "-u", str(os.getuid()), "-F", "p", "--", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError(f"Cannot inspect lock {path}: {result.stderr.strip()}")
    holders = set()
    for line in result.stdout.splitlines():
        if not line.startswith("p"):
            continue
        pid = int(line[1:])
        if sys.platform == "linux":
            try:
                arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            for mode in ("shared", "exclusive"):
                expected = [
                    os.fsencode(value) for value in ("-c", runner_code, str(path), mode)
                ]
                if arguments[1:5] == expected:
                    holders.add(pid)
                    break
            continue
        if sys.platform != "darwin":
            raise RuntimeError("Process inspection supports macOS and Linux only")
        command = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
        if command.returncode == 1 and not command.stdout.strip():
            continue
        command.check_returncode()
        # macOS lsof cannot distinguish flock holders from ordinary file readers.
        # Match the launcher's complete code as well as the open lock file.
        arguments = " ".join(command.stdout.replace(r"\012", "\n").split())
        for mode in ("shared", "exclusive"):
            signature = " ".join(f" -c {runner_code} {path} {mode} ".split())
            if f" {signature} " in f" {arguments} ":
                holders.add(pid)
                break
    return holders


def managed_processes(config, selected, processes, runner_code):
    accounts = Path(config) / "accounts"
    if accounts.is_symlink():
        raise RuntimeError("Profile directories must not be symlinks")
    directories = [accounts / selected] if selected else sorted(accounts.glob("*"))
    found = {}
    for directory in directories:
        if directory.is_symlink():
            raise RuntimeError(f"Profile directory must not be a symlink: {directory}")
        lock = directory / "full-login.lock"
        if lock.is_symlink():
            raise RuntimeError(f"Login lock must not be a symlink: {lock}")
        if not lock.is_file():
            continue
        # Preserve the shell launcher's spelling, including a trailing XDG slash.
        lock_argument = f"{config}/accounts/{directory.name}/full-login.lock"
        roots = lock_holders(lock_argument, runner_code) & processes.keys()
        for root in roots:
            found[root] = (directory.name, root, 0)
        depth = 1
        parents = roots
        while parents:
            children = {
                pid
                for pid, data in processes.items()
                if data[0] in parents and pid not in found
            }
            for pid in children:
                root = found[processes[pid][0]][1]
                found[pid] = (directory.name, root, depth)
            parents = children
            depth += 1
    return found


def main():
    action, config, selected = sys.argv[1:]
    runner_code = sys.stdin.read().strip()
    if not runner_code:
        raise RuntimeError("Missing login lock runner signature")
    processes = process_table()
    targets = managed_processes(config, selected, processes, runner_code)
    if not targets:
        print("No managed processes found.")
        return
    print("PROFILE\tPID\tPPID\tTTY\tROLE\tCOMMAND", flush=True)
    for pid, (profile, root, _) in sorted(targets.items()):
        parent, tty, _, command = processes[pid]
        role = "lock-holder" if pid == root else "child"
        print(f"{profile}\t{pid}\t{parent}\t{tty}\t{role}\t{command}", flush=True)
    if action == "ps":
        return
    ancestors = set()
    current = os.getpid()
    while current in processes and current not in ancestors:
        ancestors.add(current)
        current = processes[current][0]
    if ancestors & targets.keys():
        raise RuntimeError(
            "Run kill from a separate terminal outside the target session"
        )
    # Recheck membership and start time after discovery; never act on a saved PID alone.
    fresh = process_table()
    active = managed_processes(config, selected, fresh, runner_code)
    killed = 0
    for pid in sorted(targets, key=lambda item: targets[item][2], reverse=True):
        if pid not in active or fresh.get(pid) != processes[pid]:
            continue
        latest = process_table()
        if latest.get(pid) != processes[pid]:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass
    print(f"Sent SIGKILL to {killed} managed processes.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
