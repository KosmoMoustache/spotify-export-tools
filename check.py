#!/usr/bin/env python3
"""Check which tracks from .csv exist in Subsonic server.

Usage:
  uv run check.py --csv "My_Playlist.csv" \
          --url http://localhost:4533 --username <user> --password <pass> \
                [--out results.txt]
                [--format flac] [--auto-skip]
                [--cache <file>] [--no-cache]

When a track is not found it is presented interactively with the search
candidates so you can pick a match, search again, skip, or abort (pass
--auto-skip to skip such tracks without prompting). A match confirmed this way
is logged in the cache file (default: sync_cache.txt) as a link between the
original CSV query and the title/artist found on the server (no song id is
stored), and on later runs is used as a fallback whenever the same track is not
found by searching.

Credentials (url/username/password) are resolved with this precedence:
command line > config file (> creds.txt by default) > environment
variables (NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD) > default URL.
The config file is a simple key=value text file:

  url = http://localhost:4533
  username = myuser
  password = mypass
"""

import csv
import os
import sys
import time
from dataclasses import dataclass

from SubsonicClient import SubsonicClient
from cli import CLI, Config, server_from_args
from common import (
    AbortSync,
    artist_matches,
    interactive_resolve,
    load_cache,
    log_cached_match,
    resolve_from_cache as common_resolve_from_cache,
    save_cache,
    score_song,
    title_matches,
)


@dataclass
class CheckConfig(Config):
    out_path: str = "check-results.txt"
    quiet: bool = False
    format: str = "flac"


class CheckCLI(CLI):
    description = __doc__

    def add_arguments(self):
        self.parser.add_argument(
            "--out",
            default="check-results.txt",
            help="results output file (default: check-results.txt)",
        )
        self.parser.add_argument(
            "--format",
            default="flac",
            help="expected audio format for found tracks, e.g. flac, mp3; "
            "tracks with a different content format are reported as FORMAT-MISMATCH (default: flac)",
        )
        self.parser.add_argument(
            "--quiet", action="store_true", help="suppress per-track progress output"
        )

    def build_config(self, args) -> CheckConfig:
        return CheckConfig(
            csv_path=args.csv,
            server=server_from_args(self.parser, args),
            auto_skip=args.auto_skip,
            cache_path=args.cache,
            no_cache=args.no_cache,
            out_path=args.out,
            quiet=args.quiet,
            format=args.format.casefold(),
        )


def parse_args() -> CheckConfig:
    return CheckCLI().parse()


def evaluate_track(client: SubsonicClient, row: dict):
    title = row.get("Track Name", "").strip()
    artists = row.get("Artist Name(s)", "").strip()
    query = f"{title} {artists}".replace(";", " ")
    matches = client.search_tracks(query, song_count=20)
    good = []
    for song in matches:
        nav_title = song.get("title", "")
        nav_artist = song.get("artist", "")
        if artist_matches(artists, nav_artist) and title_matches(title, nav_title):
            good.append(song)
    return good


def prompt_resolve(client: SubsonicClient, cfg: Config, row: dict) -> list:
    """Ask the user to pick a match for a track that was not found.

    Returns a one-element list with the chosen song, or [] if the track was
    skipped (or omitted automatically with --auto-skip). Raises AbortSync when
    the user aborts.
    """
    if cfg.auto_skip:
        return []
    title = (row.get("Track Name") or "").strip()
    album = (row.get("Album Name") or "").strip()
    artists = (row.get("Artist Name(s)") or "").strip()
    query = f"{title} {artists}".replace(";", " ")

    def search(q: str) -> list:
        return client.search_tracks(q, song_count=20)

    def rank(songs: list) -> list:
        return sorted(
            (s for s in songs if s.get("id")),
            key=lambda s: score_song(row, s),
            reverse=True,
        )

    song = interactive_resolve(
        f"'{title} - {album} by {artists}' — Choose:",
        search(query),
        search,
        query,
        rank=rank,
    )
    return [song] if song else []


def resolve_from_cache(
    client: SubsonicClient, i: int, total: int, query: str, cache: dict
) -> list:
    """Resolve a query from a previously logged match (query -> title|||artist)."""
    song = common_resolve_from_cache(client, f"[{i}/{total}]", query, cache)
    return [song] if song else []


# Maps MIME subtypes (and noisy variants) to canonical format names.
_MIME_ALIASES = {
    "mpeg": "mp3",
    "mpeg3": "mp3",
    "x-mpeg": "mp3",
    "x-mpeg-3": "mp3",
    "mpeg-3": "mp3",
    "x-flac": "flac",
    "x-wav": "wav",
    "x-ogg": "ogg",
    "vorbis": "ogg",
    "x-m4a": "m4a",
    "mp4": "m4a",
    "audio": "",  # bare 'audio' subtype should never win
}


def content_format_of(song: dict) -> str:
    if not song:
        return ""
    for key in ("suffix", "contentType"):
        val = str(song.get(key) or "").strip()
        if not val:
            continue
        name = val.split(";")[0].split("/")[-1].casefold()
        name = _MIME_ALIASES.get(name, name)
        if name:
            return name
    return ""


def format_matches(song: dict, expected: str) -> bool:
    if not expected:
        return True
    return content_format_of(song) == expected


def build_text(cfg: Config, rows: list, results: list) -> str:
    lines = []
    total = len(rows)
    found = sum(1 for r in results if r["found"])
    missing = total - found
    lines.append(
        f"Checked {total} tracks against {cfg.server.base_url}: {found} found, {missing} missing."
    )
    if cfg.format:
        fmt_matches = sum(
            1 for r in results if r["found"] and format_matches(r["song"], cfg.format)
        )
        fmt_bad = found - fmt_matches
        lines.append(
            f"Format check ({cfg.format}): {fmt_matches} ok, {fmt_bad} with a different format."
        )
    lines.append("=" * 72)
    for row, res in zip(rows, results):
        title = row["Track Name"]
        artists = row["Artist Name(s)"]
        if res["found"]:
            song = res["song"]
            fmt = content_format_of(song)
            if cfg.format and not format_matches(song, cfg.format):
                status = f"FORMAT-MISMATCH (found {fmt}, expected {cfg.format})"
            else:
                status = "FOUND"
            lines.append(f"[{status}] {title} - {artists}")
            lines.append(f"       Navidrome ID: {res['match_id']}")
            if fmt:
                lines.append(f"       format: {fmt}")
        elif res["error"]:
            lines.append(f"[ERROR] {title} - {artists}")
            lines.append(f"       error: {res['error']}")
        else:
            lines.append(f"[MISSING] {title} - {artists}")
        lines.append("")
    return "\n".join(lines), found, missing


def main() -> int:
    cfg = parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if not os.path.exists(cfg.csv_path):
        print(f"CSV not found: {cfg.csv_path}", file=sys.stderr)
        return 1

    client = SubsonicClient(cfg.server, client_name="check")

    with open(cfg.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    cache = load_cache(cfg.cache_path) if not cfg.no_cache else {}
    if cache:
        print(
            f"Loaded {len(cache)} cached query->title/artist links from {cfg.cache_path}."
        )

    results = []
    found_so_far = 0
    missing_so_far = 0
    fmt_bad_so_far = 0
    for i, row in enumerate(rows, start=1):
        title = (row.get("Track Name") or "").strip()
        artists = (row.get("Artist Name(s)") or "").strip()
        if not title:
            results.append(
                {
                    "found": False,
                    "match_id": None,
                    "error": "no track name",
                    "song": None,
                }
            )
        else:
            try:
                query = f"{title} {artists}".replace(";", " ")
                good = evaluate_track(client, row)
                if not good and not cfg.no_cache:
                    good = resolve_from_cache(client, i, len(rows), query, cache)
                if not good:
                    good = prompt_resolve(client, cfg, row)
                    if good and not cfg.no_cache:
                        log_cached_match(f"[{i}/{len(rows)}]", query, good[0], cache)
                if good:
                    song = good[0]
                    fmt = content_format_of(song)
                    if cfg.format and not format_matches(song, cfg.format):
                        fmt_bad_so_far += 1
                    results.append(
                        {
                            "found": True,
                            "match_id": song.get("id"),
                            "error": None,
                            "song": song,
                        }
                    )
                else:
                    results.append(
                        {"found": False, "match_id": None, "error": None, "song": None}
                    )
            except AbortSync:
                print("\nAborted by user.")
                if not cfg.no_cache:
                    save_cache(cfg.cache_path, cache)
                body, found, missing = build_text(cfg, rows, results)
                with open(cfg.out_path, "w", encoding="utf-8", newline="") as f:
                    f.write(body)
                print(f"Partial results written to {cfg.out_path}")
                return 130
            except Exception as exc:
                print(
                    f"Warning: error while checking '{title}' - {exc}", file=sys.stderr
                )
                results.append(
                    {"found": False, "match_id": None, "error": str(exc), "song": None}
                )
        if results[-1]["found"]:
            found_so_far += 1
        else:
            missing_so_far += 1
        time.sleep(cfg.server.delay)
        if not cfg.quiet:
            song = results[-1]["song"]
            fmt = content_format_of(song) if song else ""
            status = "FOUND" if results[-1]["found"] else "MISS"
            if cfg.format and fmt and fmt != cfg.format:
                status = "FORMAT"
            print(
                f"[{i}/{len(rows)}] {status} {title} - {artists} "
                f"(found={found_so_far}, missing={missing_so_far})"
                + (f" [{fmt}]" if fmt else "")
            )

    if not cfg.no_cache:
        n = save_cache(cfg.cache_path, cache)
        print(f"Saved {n} mapping(s) to {cfg.cache_path}.")

    body, found, missing = build_text(cfg, rows, results)
    with open(cfg.out_path, "w", encoding="utf-8", newline="") as f:
        f.write(body)

    print(
        f"Done. {found} found, {len(results) - found} not found. Results written to {cfg.out_path}"
    )
    if cfg.format and fmt_bad_so_far:
        print(f"Tracks with a format other than '{cfg.format}':")
        for row, res in zip(rows, results):
            if (
                res["found"]
                and res["song"]
                and content_format_of(res["song"]) != cfg.format
            ):
                print(
                    f"  - {row['Track Name']} - {row['Artist Name(s)']} "
                    f"[{content_format_of(res['song'])}]"
                )
    if missing:
        print("Missing tracks (not found in Navidrome):")
        for row, res in zip(rows, results):
            if not res["found"]:
                print(f"  - {row['Track Name']} - {row['Artist Name(s)']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
