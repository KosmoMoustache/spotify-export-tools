#!/usr/bin/env python3
"""Single entry point for the Spotify/Subsonic tooling.

Commands:
  test    test connectivity/auth with the Subsonic server (ping)
  check   check which tracks from a Spotify CSV are missing on the server
          (forwards to check.py)
  sync    create/update a Subsonic playlist from a Spotify CSV export
          (forwards to sync.py)

All remaining arguments are forwarded verbatim to the underlying script, so
running `subify.py sync --help` is identical to `python sync.py
--help`. Credentials (url/username/password) are resolved with precedence:
command line > config file (creds.txt by default) > NAVIDROME_* env vars.

Usage:
  uv run subify.py test  [--config <file>] [--url URL] [--username U] [--password P]
  uv run subify.py check [args...]
  uv run subify.py sync  [args...]
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import sockseek
import check
import sync
from common import add_server_options, server_from_args

USAGE = f"""\
Usage:
  uv run subify.py test [args...]
  uv run subify.py check [args...]
  uv run subify.py sockseek  [args...]
  uv run subify.py sync  [args...]

Commands:
  test     test connectivity/auth with the Subsonic server
  check    check which tracks from a Spotify CSV are missing on the server
  sockseek generate sockseek input CSV file
  sync     create/update a Subsonic playlist from a Spotify CSV export

Run 'subify.py <command> --help' for that command's options."""


def _run(module, program: str, argv: list) -> int:
    sys.argv = [program, *argv]
    return module.main()


def cmd_test(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="test", description="Test connectivity/auth with the Subsonic server.")
    add_server_options(parser)
    args = parser.parse_args(argv)
    server = server_from_args(parser, args)

    print(f"Testing connection to {server.base_url}...")
    params = {
        "u": server.username,
        "p": server.password,
        "v": "1.16.1",
        "c": "subify",
        "f": "json",
    }
    req = urllib.request.Request(f"{server.base_url}/rest/ping?{urllib.parse.urlencode(params)}")
    try:
        with urllib.request.urlopen(req, timeout=server.timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        print(f"FAILED: HTTP error {exc.code}: {exc.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"FAILED: cannot reach {server.base_url}: {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    response = json.loads(raw).get("subsonic-response", {})
    status = response.get("status")
    if status == "ok":
        print(f"OK: connected as '{server.username}', Subsonic API version {response.get('version', '?')}")
        return 0
    print(f"FAILED: status={status}, error={response.get('error', {}).get('message')}",
          file=sys.stderr)
    return 1


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd in ("test",):
        return cmd_test(rest)
    if cmd in ("sockseek",):
        return _run(sockseek, "sockseek.py", rest)
    if cmd in ("check",):
        return _run(check, "check.py", rest)
    if cmd in ("sync",):
        return _run(sync, "sync.py", rest)
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())