#!/usr/bin/env python3
"""Start one or more servers, wait until their ports accept, run a command, tear down.

The point is the teardown. A dev server launched from an agent's shell outlives
the turn that started it, so the next run finds the port taken and silently
tests the *previous* build -- the worst failure mode available, because it looks
like a pass. This script owns the lifetime: servers die when the command exits,
including on failure, timeout or Ctrl-C.

Each server runs in its own process group so that shell wrappers (``npm run``,
``manage.py runserver``) take their children down with them.

Usage:
    python scripts/with_server.py --server "npm run dev" --port 5173 \
        -- python drive_it.py

    python scripts/with_server.py \
        --server "python manage.py runserver 8137 --noreload" --port 8137 \
        --server "npm run dev" --port 5173 \
        -- python drive_it.py

Exit code is the command's own, or 1 if a server never became ready.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

READY_POLL_S = 0.1


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="with_server.py",
        description="Run a command with servers up, then guarantee they come down.",
        epilog=(
            "The command to run goes after a '--' separator, e.g.:\n"
            '  with_server.py --server "npm run dev" --port 5173 -- python drive.py'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--server",
        action="append",
        default=[],
        metavar="CMD",
        help="shell command that starts a server (repeatable, paired with --port)",
    )
    parser.add_argument(
        "--port",
        action="append",
        type=int,
        default=[],
        metavar="N",
        help="port that server listens on (repeatable, paired with --server)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="host to poll for readiness (default 127.0.0.1)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to wait for each port (default 60)",
    )
    parser.add_argument("--cwd", default=None, help="working directory for the server commands")
    # --help must work on its own, before any complaint about the separator:
    # the skill tells you to run it first, so it cannot depend on already
    # knowing the calling convention.
    if not argv or "-h" in argv or "--help" in argv:
        parser.print_help()
        raise SystemExit(0)
    if "--" not in argv:
        parser.error("missing '--' separator before the command to run")
    split = argv.index("--")
    args = parser.parse_args(argv[:split])
    command = argv[split + 1 :]
    if not command:
        sys.exit("error: no command given after '--'")
    if len(args.server) != len(args.port):
        sys.exit(
            f"error: {len(args.server)} --server but {len(args.port)} --port; "
            "each server needs exactly one port"
        )
    if not args.server:
        sys.exit("error: at least one --server/--port pair is required")
    return args, command


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def wait_for(host: str, port: int, timeout: float, proc: subprocess.Popen, log: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(host, port):
            return True
        if proc.poll() is not None:
            print(f"!! server exited early (code {proc.returncode}) before {host}:{port} opened")
            tail(log)
            return False
        time.sleep(READY_POLL_S)
    print(f"!! timed out after {timeout}s waiting for {host}:{port}")
    tail(log)
    return False


def tail(path: str, lines: int = 25) -> None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            content = handle.read().splitlines()
    except OSError:
        return
    if content:
        print(f"--- last {min(lines, len(content))} lines of server output ---")
        for line in content[-lines:]:
            print("   ", line)


def shut_down(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is not None:
            continue
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.monotonic() + 5
    for proc in procs:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def main(argv: list[str]) -> int:
    args, command = parse_args(argv)

    for port in args.port:
        if port_open(args.host, port):
            print(
                f"error: {args.host}:{port} is already in use. Something is still "
                "running -- stop it first, or the run would test whatever is "
                "already there rather than what you just started."
            )
            return 1

    procs: list[subprocess.Popen] = []
    logs: list[str] = []
    try:
        for cmd, port in zip(args.server, args.port, strict=True):
            handle = tempfile.NamedTemporaryFile(
                prefix=f"server-{port}-", suffix=".log", delete=False, mode="w"
            )
            logs.append(handle.name)
            print(f"starting {cmd!r} (port {port}, log {handle.name})")
            procs.append(
                subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=args.cwd,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )

        for proc, port, log in zip(procs, args.port, logs, strict=True):
            if not wait_for(args.host, port, args.timeout, proc, log):
                return 1
            print(f"ready: {args.host}:{port}")

        print(f"running: {' '.join(command)}")
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 130
    finally:
        shut_down(procs)
        print("servers stopped")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
