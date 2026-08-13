#!/usr/bin/env python3
"""Sync a Spotify CSV playlist export to a Subsonic/Navidrome playlist.

Reads a Spotify "playlist export" CSV, checks the target playlist on the remote
server, then resolves every track (in CSV order) to a Subsonic song ID. When a
track has no confident match, the search is retried once using only the track
name (without the artist), and the results are presented for approval. Tracks
that still cannot be matched are resolved interactively (pick a candidate,
search again, skip, or abort). When done, the playlist is created or updated so
that its entries are exactly the resolved tracks, in CSV order.

Usage:
  uv run sync.py --csv "My_Playlist.csv" \
      --url http://localhost:4533 --username <user> --password <pass> \
      [--playlist-id <id> | --playlist-name "My Playlist"] \
      [--library <id-or-name> ... | --library-select] \
      [--list-libraries] [--dry-run] [--auto-skip] [--cache <file>] [--no-cache]

Credentials (url/username/password) are resolved with this precedence:
command line > config file (creds.txt by default) > environment variables
(NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD) > default URL. The config
file is a simple key=value text file:

  url = http://localhost:4533
  username = myuser
  password = mypass

Search can be restricted to specific Subsonic music libraries (folders):
  --list-libraries                    list the server's libraries and exit
  --library <id-or-name>              repeatable; only search these libraries
  --library-select                    choose libraries interactively (checkbox)

Resolved Spotify->Subsonic matches are cached in a txt file (one pair per line,
spotify key TAB song id) and reused on later runs to skip searching/prompting:
  --cache <file>                      cache file (default: sync_cache.txt)
  --no-cache                          do not read or write the cache
"""

import argparse
import csv
import os
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import questionary
import requests

from common import (AbortSync, ServerConfig, add_server_options,
                    interactive_resolve as common_interactive_resolve,
                    normalize, score_song, server_from_args)

AUTO_MATCH_THRESHOLD = 50


@dataclass
class Config:
    csv_path: str
    server: ServerConfig
    playlist_id: str
    playlist_name: str
    dry_run: bool
    auto_skip: bool
    libraries: list = field(default_factory=list)
    library_select: bool = False
    list_libraries: bool = False
    cache_path: str = "sync_cache.txt"
    no_cache: bool = False


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="Liked_Songs.csv",
                        help="Spotify playlist export CSV (default: Liked_Songs.csv)")
    add_server_options(parser, default_delay=0.15)
    parser.add_argument("--playlist-id", default="",
                        help="Subsonic playlist id to update (overrides --playlist-name)")
    parser.add_argument("--playlist-name", default="",
                        help="playlist name to find (or create); default: CSV file stem")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve tracks but do not create/update the playlist")
    parser.add_argument("--auto-skip", action="store_true",
                        help="skip unresolvable tracks without prompting")
    parser.add_argument("--library", action="append", default=[],
                        help="only search the given music library/folder "
                             "(repeatable; by numeric id or name)")
    parser.add_argument("--library-select", action="store_true",
                        help="interactively choose which music libraries to search")
    parser.add_argument("--list-libraries", action="store_true",
                        help="list the music libraries on the server and exit")
    parser.add_argument("--cache", default="sync_cache.txt",
                        help="txt file mapping Spotify tracks to Subsonic song ids "
                             "(default: sync_cache.txt)")
    parser.add_argument("--no-cache", action="store_true",
                        help="do not read or write the track-mapping cache file")
    args = parser.parse_args()
    return Config(
        csv_path=args.csv, server=server_from_args(parser, args),
        playlist_id=args.playlist_id.strip(), playlist_name=args.playlist_name.strip(),
        dry_run=args.dry_run, auto_skip=args.auto_skip,
        libraries=args.library, library_select=args.library_select,
        list_libraries=args.list_libraries,
        cache_path=args.cache, no_cache=args.no_cache,
    )


class SubsonicClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api_version = "1.16.1"
        self.client_name = "sync"
        self.session = requests.Session()
        self.music_folder_ids: list = []
        self.music_folder_names: list = []

    def _base_params(self) -> dict:
        return {
            "u": self.cfg.server.username,
            "p": self.cfg.server.password,
            "v": self.api_version,
            "c": self.client_name,
            "f": "json",
        }

    def _request(self, endpoint: str, params: dict, post: bool = False) -> dict:
        url = f"{self.cfg.server.base_url}/rest/{endpoint}"
        if self.cfg.server.verbose:
            print(f"[verbose] {('POST' if post else 'GET')} /rest/{endpoint} {params}")
        if post:
            resp = self.session.post(url, params=self._base_params(),
                                     data=params, timeout=self.cfg.server.timeout)
        else:
            resp = self.session.get(url, params={**self._base_params(), **params},
                                    timeout=self.cfg.server.timeout)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"HTTP {resp.status_code} on /rest/{endpoint}: {resp.text[:300]}") from exc
        data = resp.json().get("subsonic-response", {})
        if data.get("status") == "failed":
            raise RuntimeError(f"Subsonic error on /rest/{endpoint}: {data.get('error', {}).get('message')}")
        return data

    def ping(self) -> dict:
        return self._request("ping", {})

    def search_tracks(self, query: str, song_count: int = 10,
                      music_folder_id=None) -> list:
        params = {
            "query": query,
            "songCount": song_count,
            "artistCount": 0,
            "albumCount": 0,
        }
        if music_folder_id is not None:
            params["musicFolderId"] = music_folder_id
        data = self._request("search3", params)
        return data.get("searchResult3", {}).get("song", [])

    def search(self, query: str, song_count: int = 10) -> list:
        """Search across the configured libraries (or all libraries if none set)."""
        if not self.music_folder_ids:
            return self.search_tracks(query, song_count)
        results = []
        seen = set()
        for fid in self.music_folder_ids:
            for song in self.search_tracks(query, song_count, music_folder_id=fid):
                sid = song.get("id")
                if sid and sid not in seen:
                    seen.add(sid)
                    results.append(song)
        return results

    def get_music_folders(self) -> list:
        data = self._request("getMusicFolders", {})
        return data.get("musicFolders", {}).get("musicFolder", [])

    def get_song(self, song_id: str) -> dict:
        """Return the song with the given id, or None if it no longer exists."""
        try:
            data = self._request("getSong", {"id": song_id})
        except RuntimeError:
            return None
        return data.get("song")

    def get_playlists(self) -> list:
        data = self._request("getPlaylists", {})
        return data.get("playlists", {}).get("playlist", [])

    def get_playlist(self, playlist_id: str) -> dict:
        data = self._request("getPlaylist", {"id": playlist_id})
        return data.get("playlist", {})

    def create_playlist(self, playlist_id: str = None, name: str = None,
                        song_ids: list = None) -> dict:
        params = {}
        if playlist_id:
            params["playlistId"] = playlist_id
        if name:
            params["name"] = name
        for sid in song_ids or []:
            params.setdefault("songId", []).append(sid)
        data = self._request("createPlaylist", params, post=True)
        return data.get("playlist", {})


def load_csv_rows(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def track_key(row: dict) -> str:
    """Stable identifier for a Spotify CSV row, used as the cache key."""
    uri = (row.get("Track URI") or "").strip()
    if uri:
        return uri
    title = normalize(row.get("Track Name", ""))
    artists = normalize(row.get("Artist Name(s)", ""))
    return f"{title}|||{artists}"


def load_cache(path: str) -> dict:
    """Load spotify-key -> subsonic-song-id pairs from a txt file."""
    mapping = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or "\t" not in line:
                    continue
                key, _, song_id = line.partition("\t")
                if key.strip() and song_id.strip():
                    mapping[key.strip()] = song_id.strip()
    except OSError:
        pass
    return mapping


def save_cache(path: str, mapping: dict) -> int:
    """Write spotify-key -> subsonic-song-id pairs to a txt file."""
    entries = sorted(mapping.items())
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for key, song_id in entries:
            f.write(f"{key}\t{song_id}\n")
    return len(entries)


def best_auto_match(client: SubsonicClient, row: dict, query: str):
    title = normalize(row.get("Track Name", ""))
    artists = row.get("Artist Name(s)", "").strip()
    if not title and not artists:
        return None
    songs = client.search(query)
    if not songs:
        return None
    ranked = sorted(
        (s for s in songs if s.get("id")),
        key=lambda s: score_song(row, s),
        reverse=True,
    )
    if not ranked:
        return None
    top = ranked[0]
    if score_song(row, top) >= AUTO_MATCH_THRESHOLD:
        return top
    return None


def interactive_resolve(client: SubsonicClient, cfg: Config, row: dict,
                        query: str, title: str, album: str, artists: str):
    """Ask the user how to resolve a track that has no confident match."""
    if cfg.auto_skip:
        return None, "skipped (auto)"

    songs = client.search(query)

    def rank(songs: list) -> list:
        return sorted((s for s in songs if s.get("id")),
                      key=lambda s: score_song(row, s), reverse=True)[:8]

    song = common_interactive_resolve(f"'{title} - {album} by {artists}' — Choose:", songs,
                                      client.search, query, rank=rank)
    if song is None:
        return None, "skipped"
    return song, "manual"


def resolve_track(client: SubsonicClient, cfg: Config, row: dict, cache: dict):
    title = row.get("Track Name", "").strip()
    album = row.get("Album Name", "").strip()
    artists = row.get("Artist Name(s)", "").strip()
    if not title:
        return None, "no track name in CSV"

    if not cfg.no_cache:
        key = track_key(row)
        cached = cache.get(key)
        if cached:
            song = client.get_song(cached)
            if song:
                return song, "cache"
            cache.pop(key, None)  # stale id -> drop it and re-resolve

    query = f"{title} {artists}".replace(";", " ")
    song = best_auto_match(client, row, query)
    if song:
        if not cfg.no_cache:
            cache[key] = song["id"]
        return song, "auto"
    # No confident match with artist: retry once using only the track name,
    # then present the results for user approval.
    print(f"  no confident match for '{title}' by '{artists}'; "
          f"retrying with track name only ...")
    song, how = interactive_resolve(client, cfg, row, title, title, album, artists)
    if song is not None and not cfg.no_cache:
        cache[key] = song["id"]
    return song, how


def resolve_target_playlist(client: SubsonicClient, cfg: Config, default_name: str) -> dict:
    """Return the remote playlist to update, or None if a new one must be created."""
    if cfg.playlist_id:
        playlist = client.get_playlist(cfg.playlist_id)
        if not playlist:
            raise RuntimeError(f"Playlist id '{cfg.playlist_id}' not found on {cfg.server.base_url}")
        return playlist
    name = cfg.playlist_name or default_name
    playlists = client.get_playlists()
    for pl in playlists:
        if normalize(pl.get("name", "")) == normalize(name):
            return pl
    return None


def resolve_library_ids(cfg: Config, folders: list) -> tuple:
    """Map --library values to folder ids (by numeric id or name).

    Returns (ids, names) where ids is None when no --library restriction was
    given (i.e. search all libraries).
    """
    if not cfg.libraries:
        return None, None
    folders_by_id = {str(f.get("id")): f for f in folders}
    folders_by_name = {normalize(f.get("name", "")): f for f in folders}
    ids, names, missing = [], [], []
    for raw in cfg.libraries:
        key = raw.strip()
        folder = folders_by_id.get(key) or folders_by_name.get(normalize(key))
        if folder is None:
            missing.append(key)
            continue
        ids.append(str(folder["id"]))
        names.append(str(folder.get("name", folder["id"])))
    if missing:
        print(f"Unknown library/folder: {', '.join(missing)}. "
              f"Available: {', '.join(f.get('name') for f in folders) or 'none'}",
              file=sys.stderr)
        raise SystemExit(2)
    return ids, names


def choose_libraries_interactive(folders: list):
    """Let the user pick the libraries to query (checkbox, all pre-selected)."""
    choices = [{"name": f"{f.get('name', '?')} (id={f.get('id')})",
                "value": str(f["id"]), "checked": True} for f in folders]
    try:
        selected = questionary.checkbox(
            "Select music libraries to search (space toggles, enter to confirm):",
            choices=choices,
        ).unsafe_ask()
    except KeyboardInterrupt:
        raise AbortSync() from None
    if not selected:
        return None, None  # empty selection == search all
    names = [next(f.get("name") for f in folders if str(f.get("id")) == sid)
             for sid in selected]
    return selected, names


def configure_libraries(client: SubsonicClient, cfg: Config) -> None:
    """Resolve the --library / --library-select options into client search filters."""
    folders = client.get_music_folders()
    if cfg.list_libraries:
        print(f"Music libraries on {cfg.server.base_url}:")
        for f in folders:
            print(f"  {f.get('id')}  {f.get('name', '?')}")
        if not folders:
            print("  (none reported by the server)")
        raise SystemExit(0)
    ids, names = None, None
    if cfg.library_select:
        print("Choosing music libraries interactively:")
        ids, names = choose_libraries_interactive(folders)
    else:
        ids, names = resolve_library_ids(cfg, folders)
    if ids:
        client.music_folder_ids = ids
        client.music_folder_names = names
        print(f"Search restricted to {len(ids)} music library/libraries: "
              f"{', '.join(names)}")


def diff_positions(existing_ids: list, new_ids: list) -> dict:
    """Compare existing vs desired order; returns counts for the summary."""
    existing_counts = Counter(existing_ids)
    new_counts = Counter(new_ids)
    common = sum((existing_counts & new_counts).values())
    added = len(new_ids) - common
    removed = len(existing_ids) - common

    moved = 0
    for sid in set(existing_ids) & set(new_ids):
        old_pos = [i for i, x in enumerate(existing_ids) if x == sid]
        new_pos = [i for i, x in enumerate(new_ids) if x == sid]
        if old_pos != new_pos:
            moved += 1
    return {"added": added, "removed": removed, "moved": moved,
            "already": common, "old_total": len(existing_ids),
            "new_total": len(new_ids)}


def main() -> int:
    cfg = parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    print(f"Connecting to {cfg.server.base_url} ...")
    client = SubsonicClient(cfg)
    try:
        ping = client.ping()
    except Exception as exc:
        print(f"FAILED: cannot reach {cfg.server.base_url}: {exc}", file=sys.stderr)
        return 1
    print(f"OK: connected as '{cfg.server.username}', Subsonic API version {ping.get('version', '?')}")

    try:
        configure_libraries(client, cfg)
    except SystemExit as exc:
        return int(exc.code or 0)

    if not os.path.exists(cfg.csv_path):
        print(f"CSV not found: {cfg.csv_path}", file=sys.stderr)
        return 1

    rows = load_csv_rows(cfg.csv_path)
    if not rows:
        print(f"No tracks found in {cfg.csv_path}", file=sys.stderr)
        return 1

    default_name = Path(cfg.csv_path).stem
    target = resolve_target_playlist(client, cfg, default_name)
    if target:
        existing_ids = [e.get("id") for e in target.get("entry", []) if e.get("id")]
        print(f"Playlist '{target.get('name')}' (id={target.get('id')}) "
              f"already exists with {len(existing_ids)} tracks.")
    else:
        existing_ids = []
        name = cfg.playlist_name or default_name
        print(f"Playlist '{name}' not found on server — will be created.")

    cache = load_cache(cfg.cache_path) if not cfg.no_cache else {}
    if cache:
        print(f"Loaded {len(cache)} cached Spotify->Subsonic mappings from {cfg.cache_path}.")

    print(f"Resolving {len(rows)} tracks (CSV order) ...")
    resolved_ids = []
    skipped = []
    auto_count = 0
    manual_count = 0
    cache_count = 0
    matched = 0
    for i, row in enumerate(rows, start=1):
        try:
            song, how = resolve_track(client, cfg, row, cache)
        except AbortSync:
            print("\nAborted by user.")
            if not cfg.no_cache:
                save_cache(cfg.cache_path, cache)
            return 130
        title = row.get("Track Name", "").strip()
        artists = row.get("Artist Name(s)", "").strip()
        if song is None:
            skipped.append((title, artists, how))
            print(f"[{i}/{len(rows)}] SKIP  {title} - {artists} ({how})")
        else:
            resolved_ids.append(song["id"])
            matched += 1
            if how == "cache":
                cache_count += 1
            elif how == "auto":
                auto_count += 1
            else:
                manual_count += 1
            print(f"[{i}/{len(rows)}] OK    {title} - {artists} ({how})")
        time.sleep(cfg.server.delay)

    print()
    if not cfg.no_cache:
        n = save_cache(cfg.cache_path, cache)
        print(f"Saved {n} mapping(s) to {cfg.cache_path}.")
    if cfg.dry_run:
        print("DRY RUN: no changes were made to the server.")
        diff = diff_positions(existing_ids, resolved_ids)
        _print_summary(cfg, rows, target, resolved_ids, skipped,
                       diff, auto_count, manual_count, applied=False, client=client,
                       cache_count=cache_count)
        return 0

    diff = diff_positions(existing_ids, resolved_ids)
    if target and existing_ids == resolved_ids:
        print("Playlist is already up to date — no changes needed.")
        _print_summary(cfg, rows, target, resolved_ids, skipped,
                       diff, auto_count, manual_count, applied=False, client=client,
                       cache_count=cache_count)
        return 0

    if target:
        print(f"Updating playlist '{target.get('name')}' (id={target.get('id')}) "
              f"with {len(resolved_ids)} tracks in CSV order ...")
        try:
            result = client.create_playlist(playlist_id=target["id"], song_ids=resolved_ids)
        except Exception as exc:
            print(f"FAILED to update playlist: {exc}", file=sys.stderr)
            return 1
        applied = True
    else:
        name = cfg.playlist_name or default_name
        print(f"Creating playlist '{name}' with {len(resolved_ids)} tracks ...")
        try:
            result = client.create_playlist(name=name, song_ids=resolved_ids)
        except Exception as exc:
            print(f"FAILED to create playlist: {exc}", file=sys.stderr)
            return 1
        target = result
        applied = True

    final_id = target.get("id", "?")
    print(f"Done. Playlist id={final_id}, {len(resolved_ids)} tracks.")
    _print_summary(cfg, rows, target, resolved_ids, skipped,
                   diff, auto_count, manual_count, applied=True, client=client,
                   cache_count=cache_count)
    return 0


def _print_summary(cfg: Config, rows: list, target: dict, resolved_ids: list,
                   skipped: list, diff: dict, auto_count: int, manual_count: int,
                   applied: bool, client: SubsonicClient = None,
                   cache_count: int = 0) -> None:
    name = (target.get("name") if target else cfg.playlist_name
            or Path(cfg.csv_path).stem)
    lines = []
    lines.append("=" * 60)
    lines.append("SYNC SUMMARY")
    lines.append("=" * 60)
    lines.append(f"Playlist          : {name} (id={target.get('id', '?') if target else 'new'})")
    lines.append(f"Server            : {cfg.server.base_url}")
    lines.append(f"CSV source        : {cfg.csv_path}")
    if client and client.music_folder_ids:
        lines.append(f"Libraries searched : {', '.join(client.music_folder_names or client.music_folder_ids)}")
    lines.append(f"CSV tracks        : {len(rows)}")
    lines.append(f"Matched           : {len(resolved_ids)} "
                 f"(cache={cache_count}, auto={auto_count}, manual={manual_count})")
    lines.append(f"Skipped / not found: {len(skipped)}")
    lines.append("-" * 60)
    if target:
        lines.append(f"Already in playlist : {diff['already']}")
        lines.append(f"Added to playlist   : {diff['added']}")
        lines.append(f"Removed from playlist: {diff['removed']}")
        lines.append(f"Reordered / moved   : {diff['moved']}")
        lines.append(f"Final playlist size : {diff['new_total']} (was {diff['old_total']})")
    else:
        lines.append(f"New playlist size   : {len(resolved_ids)}")
    lines.append("-" * 60)
    lines.append(f"Changes applied to server : {'yes' if applied else 'NO (dry-run / no change)'}")
    if skipped:
        lines.append("Skipped tracks:")
        for title, artists, how in skipped:
            lines.append(f"  - {title} - {artists} ({how})")
    lines.append("=" * 60)
    print("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
