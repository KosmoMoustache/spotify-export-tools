#!/usr/bin/env python3
"""Convert check.py output (results.txt) into a CSV feed for sldl/sockseek.

Reads the [MISSING] (and optionally [FORMAT-MISMATCH]/[ERROR]) entries from
results.txt and writes a CSV that sldl/sockseek can consume directly (column
names are auto-detected: Artist / Title / Album / Length).

Usage:
  python results_to_sldl.py [--input results.txt] [--csv Liked_Songs.csv]
                            [--out sldl_tracks.csv] [--include-format-mismatch]

If --csv is provided, the exact title/artist/album/duration are taken from the
Spotify export; otherwise the title/artist pair is recovered by splitting the
result line on its last " - ". Requires Python 3.8+.
"""

import argparse
import csv
import sys
import unicodedata
from dataclasses import dataclass


@dataclass
class Config:
    input_path: str
    csv_path: str
    out_path: str
    include_format_mismatch: bool
    include_errors: bool


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="check-results.txt")
    parser.add_argument("--csv", default="Liked_Songs.csv",
                        help="Spotify export for exact title/artist/album/duration "
                             "(set to '' to parse from the result text only)")
    parser.add_argument("--out", default="sldl_tracks.csv")
    parser.add_argument("--include-format-mismatch", action="store_true",
                        help="also export FORMAT-MISMATCH tracks (found, but not the expected format)")
    parser.add_argument("--include-errors", action="store_true",
                        help="also export tracks that errored during the check")
    args = parser.parse_args()
    return Config(
        input_path=args.input, csv_path=args.csv, out_path=args.out,
        include_format_mismatch=args.include_format_mismatch,
        include_errors=args.include_errors,
    )


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").replace("\u00a0", " ").split())


def parse_result_lines(path: str) -> list[tuple[str, str]]:
    entries = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.startswith("["):
                continue
            end = line.find("]")
            if end == -1:
                continue
            status = line[1:end]
            text = line[end + 1:].strip()
            if text:
                entries.append((status, text))
    return entries


def load_spotify_rows(path: str) -> list[tuple[str, dict]]:
    def first_match(header, names):
        for idx, h in enumerate(header):
            clean = normalize(h).casefold().replace(" ", "")
            if clean in names:
                return idx
        return -1

    title_names = {"trackname", "title", "song", "songname", "tracktitle"}
    artist_names = {"artistname(s)", "artist", "artistname", "artists", "artistnames"}
    album_names = {"albumname", "album", "albumtitle"}
    duration_names = {"duration(ms)", "duration", "length", "tracklength", "durationms",
                      "songduration"}

    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        headers = next(reader)
        title_i = first_match(headers, title_names)
        artist_i = first_match(headers, artist_names)
        album_i = first_match(headers, album_names)
        duration_i = first_match(headers, duration_names)
        for row in reader:
            rec = {}
            for name, i in (("title", title_i), ("artist", artist_i),
                            ("album", album_i), ("duration", duration_i)):
                if i is not None and i < len(row):
                    rec[name] = row[i].strip()
            key = normalize(f"{rec.get('title', '')} - {rec.get('artist', '')}")
            rows.append((key, rec))
    return rows


def duration_ms_to_s(value: str) -> int:
    try:
        return int(round(float(value) / 1000))
    except (ValueError, TypeError):
        return 0


def main() -> int:
    cfg = parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    entries = parse_result_lines(cfg.input_path)
    if not entries:
        print(f"No entries parsed from {cfg.input_path}", file=sys.stderr)
        return 1

    wanted = {"MISSING"}
    if cfg.include_format_mismatch:
        wanted.add("FORMAT-MISMATCH")
    if cfg.include_errors:
        wanted.add("ERROR")


    def keep(status: str) -> bool:
        return status in wanted or status.split(" ")[0] in wanted

    spotify = load_spotify_rows(cfg.csv_path) if cfg.csv_path else []
    by_key = {}
    for key, rec in spotify:
        by_key.setdefault(key, rec)

    out = []
    unparsed = 0
    kept = [(s, t) for (s, t) in entries if keep(s)]

    for status, text in kept:
        rec = by_key.get(normalize(text))
        if rec:
            title = rec.get("title", "")
            artist = rec.get("artist", "")
            album = rec.get("album", "")
            duration = duration_ms_to_s(rec["duration"]) if "duration" in rec else 0
        else:
            split = text.rsplit(" - ", 1)
            if len(split) == 2:
                title, artist = split
            else:
                title, artist = text, ""
            album = ""
            duration = 0
            unparsed += 1
        if not title and not artist:
            continue
        out.append((artist, title, album, duration))

    with open(cfg.out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Artist", "Title", "Album", "Length"])
        for artist, title, album, duration in out:
            writer.writerow([artist, title, album, duration])

    print(f"{len(out)} tracks written to {cfg.out_path} "
          f"({', '.join(sorted(wanted))}).")
    if unparsed:
        print(f"{unparsed} entries could not be matched to a Spotify row and "
              f"were parsed from the result text.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())