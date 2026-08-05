from __future__ import annotations

import argparse
import sys

from .sessions import commands as session_commands
from .usage import commands as usage_commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified Codex usage monitor and session archive")
    sub = parser.add_subparsers(dest="domain", required=True)

    usage = sub.add_parser("usage", help="Collect and inspect Codex usage quota snapshots")
    usage.set_defaults(_domain_main=usage_commands.main)
    usage_commands.configure_parser(usage)

    sessions = sub.add_parser("sessions", help="Import, verify, search and export Codex sessions")
    sessions.set_defaults(_domain_main=session_commands.main)
    session_commands.configure_parser(sessions)

    status = sub.add_parser("status", help="Print combined application status as JSON")
    status.set_defaults(_combined_command="status")

    verify = sub.add_parser("verify", help="Verify the session archive")
    verify.add_argument("--deep", action="store_true")
    verify.set_defaults(_combined_command="verify")

    export = sub.add_parser("export", help="Export sessions with the session export engine")
    session_commands.add_search_args(export, include_limit=True)
    export.add_argument("--output")
    export.set_defaults(_combined_command="export")

    validate_export = sub.add_parser("validate-export", help="Validate a session export tarball")
    validate_export.add_argument("export_path")
    validate_export.set_defaults(_combined_command="validate-export")

    search = sub.add_parser("search", help="Search archived sessions")
    session_commands.add_search_args(search, include_limit=True)
    search.set_defaults(_combined_command="search")

    systemd = sub.add_parser("systemd", help="Install or remove the session archive user timer")
    systemd_sub = systemd.add_subparsers(dest="systemd_command", required=True)
    install = systemd_sub.add_parser("install-user", help="Install user timer")
    install.add_argument("--archive-root", default=str(session_commands.DEFAULT_ARCHIVE_ROOT))
    install.add_argument("--script-path", default=str(session_commands.DEFAULT_UNIFIED_SCRIPT_PATH))
    install.set_defaults(_combined_command="systemd-install-user")
    uninstall = systemd_sub.add_parser("uninstall-user", help="Remove user timer")
    uninstall.set_defaults(_combined_command="systemd-uninstall-user")
    return parser


def _run_combined(args: argparse.Namespace) -> int:
    command = args._combined_command
    if command == "status":
        usage_code = usage_commands.main(["status"])
        sessions_code = session_commands.main(["status"])
        return 0 if usage_code == 0 and sessions_code == 0 else 1
    if command == "verify":
        argv = ["verify"]
        if args.deep:
            argv.append("--deep")
        return session_commands.main(argv)
    if command == "export":
        argv = session_commands.argv_from_search_args("export", args)
        if args.output:
            argv.extend(["--output", args.output])
        return session_commands.main(argv)
    if command == "validate-export":
        return session_commands.main(["validate-export", args.export_path])
    if command == "search":
        return session_commands.main(session_commands.argv_from_search_args("search", args))
    if command == "systemd-install-user":
        return session_commands.main(
            [
                "--archive-root",
                args.archive_root,
                "install-user-systemd",
                "--script-path",
                args.script_path,
            ]
        )
    if command == "systemd-uninstall-user":
        return session_commands.main(["uninstall-user-systemd"])
    raise AssertionError(command)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "usage":
        return int(usage_commands.main(argv[1:]))
    if argv and argv[0] == "sessions":
        return int(session_commands.main(argv[1:]))
    args = build_parser().parse_args(argv)
    return _run_combined(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
