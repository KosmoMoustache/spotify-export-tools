#!/usr/bin/env python3
"""Check which tracks from .csv exist in Subsonic server.

Usage:
  python check.py [--csv Liked_Songs.csv] [--out results.txt]
                            [--format flac] [--auto-skip]

When a track is not found it is presented interactively with the search
candidates so you can pick a match, search again, skip, or abort (pass
--auto-skip to skip such tracks without prompting).

Credentials (url/username/password) are resolved with this precedence:
command line > config file (> creds.txt by default) > environment
variables (NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD) > default URL.
The config file is a simple key=value text file:

  url = http://localhost:4533
  username = myuser
  password = mypass

Uses the Navidrome/Subsonic REST API (/rest/search3). Requires Python 3.8+.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass

from common import (AbortSync, ServerConfig, add_server_options,
                    interactive_resolve, normalize, score_song, server_from_args)


@dataclass
class Config:
    csv_path: str
    out_path: str
    server: ServerConfig
    quiet: bool
    auto_skip: bool
    format: str


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="Liked_Songs.csv")
    parser.add_argument("--out", default="check-results.txt")
    add_server_options(parser)
    parser.add_argument("--quiet", action="store_true",
                        help="suppress per-track progress output")
    parser.add_argument("--auto-skip", action="store_true",
                        help="skip tracks without prompting when no match is found")
    parser.add_argument("--format", default="flac",
                        help="expected audio format for found tracks, e.g. flac; "
                             "tracks with a different content format are reported as FORMAT-MISMATCH")
    args = parser.parse_args()
    return Config(
        csv_path=args.csv, out_path=args.out,
        server=server_from_args(parser, args),
        quiet=args.quiet, auto_skip=args.auto_skip, format=args.format.casefold(),
    )


def _percent_encode(s: str, safe: str = "", encoding: str = None, errors: str = None) -> str:
    return urllib.parse.quote(s, safe=safe, encoding=encoding or "utf-8", errors=errors or "strict")


class NavidromeClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api_version = "1.16.1"
        self.client_name = "check"

    def _base_params(self) -> dict:
        return {
            "u": self.cfg.server.username,
            "p": self.cfg.server.password,
            "v": self.api_version,
            "c": self.client_name,
            "f": "json",
        }

    def _get(self, path: str, params: dict) -> dict:
        query = {**self._base_params(), **params}
        qs = urllib.parse.urlencode(query, quote_via=_percent_encode)
        url = f"{self.cfg.server.base_url}{path}?{qs}"
        if self.cfg.server.verbose:
            print(f"[verbose] GET {path}?{urllib.parse.urlencode(params, quote_via=_percent_encode)}")
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.cfg.server.timeout) as resp:
            raw = resp.read().decode("utf-8")
        if self.cfg.server.verbose:
            print(f"[verbose] {raw[:400]}")
        return json.loads(raw)

    def search_tracks(self, query: str, song_count: int = 10) -> list:
        data = self._get("/rest/search3", {
            "query": query,
            "songCount": song_count,
            "artistCount": 0,
            "albumCount": 0,
        })
        result = data.get("subsonic-response", {}).get("searchResult3", {})
        return result.get("song", [])

    def ping(self) -> dict:
        data = self._get("/rest/ping", {})
        return data.get("subsonic-response", {})


def title_matches(spotify_title: str, nav_title: str) -> bool:
    a, b = normalize(spotify_title), normalize(nav_title)
    if not b or not a:
        return False
    return a in b or b in a or a == b


def artist_matches(spotify_artist: str, nav_artist: str) -> bool:
    a, b = normalize(spotify_artist), normalize(nav_artist)
    if not a or not b:
        return a == b
    a_parts = {p for p in a.split(";") if p}
    b_words = set(b.split())
    return any(ap in b or b in ap for ap in a_parts) or bool(b_words & {w for p in a_parts for w in p.split()})


def evaluate_track(client: NavidromeClient, row: dict):
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


def prompt_resolve(client: NavidromeClient, cfg: Config, row: dict) -> list:
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
        return sorted((s for s in songs if s.get("id")),
                      key=lambda s: score_song(row, s), reverse=True)

    song = interactive_resolve(f"'{title} - {album} by {artists}' — Choose:", search(query), search,
                               query, rank=rank)
    return [song] if song else []


# Maps MIME subtypes (and noisy variants) to canonical format names.
_MIME_ALIASES = {
    "mpeg": "mp3", "mpeg3": "mp3", "x-mpeg": "mp3", "x-mpeg-3": "mp3", "mpeg-3": "mp3",
    "x-flac": "flac",
    "x-wav": "wav",
    "x-ogg": "ogg", "vorbis": "ogg",
    "x-m4a": "m4a", "mp4": "m4a",
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
    lines.append(f"Checked {total} tracks against {cfg.server.base_url}: {found} found, {missing} missing.")
    if cfg.format:
        fmt_matches = sum(1 for r in results if r["found"] and format_matches(r["song"], cfg.format))
        fmt_bad = found - fmt_matches
        lines.append(f"Format check ({cfg.format}): {fmt_matches} ok, {fmt_bad} with a different format.")
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

    client = NavidromeClient(cfg)

    with open(cfg.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    results = []
    found_so_far = 0
    missing_so_far = 0
    fmt_bad_so_far = 0
    for i, row in enumerate(rows, start=1):
        title = (row.get("Track Name") or "").strip()
        artists = (row.get("Artist Name(s)") or "").strip()
        if not title:
            results.append({"found": False, "match_id": None, "error": "no track name", "song": None})
        else:
            try:
                good = evaluate_track(client, row)
                if not good:
                    good = prompt_resolve(client, cfg, row)
                if good:
                    song = good[0]
                    fmt = content_format_of(song)
                    if cfg.format and not format_matches(song, cfg.format):
                        fmt_bad_so_far += 1
                    results.append({"found": True, "match_id": song.get("id"), "error": None, "song": song})
                else:
                    results.append({"found": False, "match_id": None, "error": None, "song": None})
            except AbortSync:
                print("\nAborted by user.")
                body, found, missing = build_text(cfg, rows, results)
                with open(cfg.out_path, "w", encoding="utf-8", newline="") as f:
                    f.write(body)
                print(f"Partial results written to {cfg.out_path}")
                return 130
            except Exception as exc:
                print(f"Warning: error while checking '{title}' - {exc}", file=sys.stderr)
                results.append({"found": False, "match_id": None, "error": str(exc), "song": None})
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
            print(f"[{i}/{len(rows)}] {status} {title} - {artists} "
                  f"(found={found_so_far}, missing={missing_so_far})" + (f" [{fmt}]" if fmt else ""))

    body, found, missing = build_text(cfg, rows, results)
    with open(cfg.out_path, "w", encoding="utf-8", newline="") as f:
        f.write(body)

    print(f"Done. {found} found, {len(results) - found} not found. Results written to {cfg.out_path}")
    if cfg.format and fmt_bad_so_far:
        print(f"Tracks with a format other than '{cfg.format}':")
        for row, res in zip(rows, results):
            if res["found"] and res["song"] and content_format_of(res["song"]) != cfg.format:
                print(f"  - {row['Track Name']} - {row['Artist Name(s)']} "
                      f"[{content_format_of(res['song'])}]")
    if missing:
        print("Missing tracks (not found in Navidrome):")
        for row, res in zip(rows, results):
            if not res["found"]:
                print(f"  - {row['Track Name']} - {row['Artist Name(s)']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())