#!/usr/bin/env python3
"""Generate MISSING and FORMAT-MISMATCH lists from a Spotify CSV.

By default the lists are parsed from the check.py results file
(check-results.txt by default), so no server calls are made. If that file does
not exist, or when --force-scan is given, the server is searched instead.

The results are written to a single file (wish.txt by default); pass --split
to write the two lists to separate files:

  * MISSING list          - one "TITLE ARTIST" per line for tracks not found
  * FORMAT-MISMATCH list  - one "TITLE ARTIST <format>" per line for tracks
                            found with an audio format other than expected
                            (the expected format is appended; flac by default)

Usage:
  uv run subify.py wish --csv "My_Playlist.csv" \
          --url http://localhost:4533 --username <user> --password <pass> \
                [--from-results check-results.txt] [--force-scan]
                [--out wish.txt] [--split]
                [--missing missing.txt] [--mismatch format-mismatch.txt]
                [--format flac] [--cache <file>] [--no-cache]

Credentials (url/username/password) are resolved with this precedence:
command line > config file (creds.txt by default) > environment variables
(NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD) > default URL.
"""

import csv
import os
import re
import sys
import time
from dataclasses import dataclass

from SubsonicClient import SubsonicClient
from check import content_format_of, evaluate_track, format_matches
from cli import CLI, Config, server_from_args
from common import load_cache, resolve_from_cache, save_cache


@dataclass
class MissingConfig(Config):
    out_path: str = "wish.txt"
    split: bool = False
    missing_path: str = "missing.txt"
    mismatch_path: str = "format-mismatch.txt"
    format: str = "flac"
    results_path: str = "check-results.txt"
    force_scan: bool = False


class MissingCLI(CLI):
    description = __doc__

    def add_arguments(self):
        self.parser.add_argument(
            "--out",
            default="wish.txt",
            help="combined output file for both lists (default: wish.txt; "
            "ignored when --split is used)",
        )
        self.parser.add_argument(
            "--split",
            action="store_true",
            help="write the missing and format-mismatch lists to two separate files",
        )
        self.parser.add_argument(
            "--missing",
            default="missing.txt",
            help="output file for tracks not found, with --split "
            "(default: missing.txt)",
        )
        self.parser.add_argument(
            "--mismatch",
            default="format-mismatch.txt",
            help="output file for tracks with a different audio format, with "
            "--split (default: format-mismatch.txt)",
        )
        self.parser.add_argument(
            "--format",
            default="flac",
            help="expected audio format for found tracks, e.g. flac, mp3; "
            "tracks with a different content format are reported as "
            "FORMAT-MISMATCH (default: flac)",
        )
        self.parser.add_argument(
            "--from-results",
            default="check-results.txt",
            help="check.py results file to parse instead of scanning the "
            "server (default: check-results.txt; used only when it exists "
            "unless --force-scan is passed)",
        )
        self.parser.add_argument(
            "--force-scan",
            action="store_true",
            help="scan the server instead of parsing the check results file",
        )

    def build_config(self, args) -> MissingConfig:
        return MissingConfig(
            csv_path=args.csv,
            server=server_from_args(self.parser, args),
            auto_skip=args.auto_skip,
            cache_path=args.cache,
            no_cache=args.no_cache,
            out_path=args.out,
            split=args.split,
            missing_path=args.missing,
            mismatch_path=args.mismatch,
            format=args.format.casefold(),
            results_path=args.from_results,
            force_scan=args.force_scan,
        )


def parse_args() -> MissingConfig:
    return MissingCLI().parse()


def _to_wish_line(line: str) -> str:
    """Convert a 'Title - Artist' results line to the 'TITLE ARTIST' format."""
    title, sep, artists = line.partition(" - ")
    return line if not sep else f"{title} {artists}"


def parse_results(path: str) -> tuple:
    """Parse a check.py results file into (missing_lines, mismatch_lines)."""
    missing = []
    mismatches = []
    mismatch_re = re.compile(
        r"^\[FORMAT-MISMATCH \(found [^,]+, expected ([^)]+)\)\] (.*)$"
    )
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("[MISSING] "):
                missing.append(_to_wish_line(line[len("[MISSING] ") :]))
            else:
                m = mismatch_re.match(line)
                if m:
                    expected, rest = m.group(1), m.group(2)
                    mismatches.append(f"{_to_wish_line(rest)} {expected}")
    return missing, mismatches


def write_wish_files(cfg: Config, missing: list, mismatches: list) -> None:
    def dump(path: str, lines: list) -> None:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines))
            if lines:
                f.write("\n")

    if cfg.split:
        dump(cfg.missing_path, missing)
        dump(cfg.mismatch_path, mismatches)
        written = f"{cfg.missing_path} and {cfg.mismatch_path}"
    else:
        dump(cfg.out_path, [*missing, *mismatches])
        written = cfg.out_path
    print(
        f"Done. {len(missing)} missing, {len(mismatches)} format mismatches. "
        f"Written to {written}."
    )


def main() -> int:
    cfg = parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if not cfg.force_scan and os.path.exists(cfg.results_path):
        missing, mismatches = parse_results(cfg.results_path)
        print(
            f"Parsed {cfg.results_path}: {len(missing)} missing, "
            f"{len(mismatches)} format mismatches."
        )
        write_wish_files(cfg, missing, mismatches)
        return 0

    if not os.path.exists(cfg.csv_path):
        print(f"CSV not found: {cfg.csv_path}", file=sys.stderr)
        return 1

    client = SubsonicClient(cfg.server, client_name="missing")

    with open(cfg.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    cache = load_cache(cfg.cache_path) if not cfg.no_cache else {}
    if cache:
        print(
            f"Loaded {len(cache)} cached query->title/artist links from {cfg.cache_path}."
        )

    missing = []
    mismatches = []
    for i, row in enumerate(rows, start=1):
        title = (row.get("Track Name") or "").strip()
        artists = (row.get("Artist Name(s)") or "").strip()
        if not title:
            continue
        query = f"{title} {artists}".replace(";", " ")
        good = evaluate_track(client, row)
        if not good and not cfg.no_cache:
            song = resolve_from_cache(client, f"[{i}/{len(rows)}]", query, cache)
            good = [song] if song else []
        if good:
            song = good[0]
            fmt = content_format_of(song)
            if cfg.format and not format_matches(song, cfg.format):
                mismatches.append(f"{title} {artists} {cfg.format}")
                status = "FORMAT-MISMATCH"
            else:
                status = "FOUND"
            print(
                f"[{i}/{len(rows)}] {status} {title} - {artists}"
                + (f" [{fmt}]" if fmt else "")
            )
        else:
            missing.append(f"{title} {artists}")
            print(f"[{i}/{len(rows)}] MISSING {title} - {artists}")
        time.sleep(cfg.server.delay)

    if not cfg.no_cache:
        n = save_cache(cfg.cache_path, cache)
        print(f"Saved {n} mapping(s) to {cfg.cache_path}.")

    write_wish_files(cfg, missing, mismatches)
    return 0


if __name__ == "__main__":
    sys.exit(main())