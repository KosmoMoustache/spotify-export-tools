"""Shared helpers for the Subify/Sonic tooling.

Centralizes the common server/credential configuration used by every script:
the --config/--url/--username/--password/--timeout/--delay/--verbose options,
credential resolution (command line > config file > NAVIDROME_* env vars >
defaults) and the ServerConfig dataclass.
"""

import os
import unicodedata
from dataclasses import dataclass

import questionary

DEFAULT_CONFIG = "creds.txt"
DEFAULT_TIMEOUT = 10.0
DEFAULT_DELAY = 0.2


@dataclass
class ServerConfig:
    base_url: str
    username: str
    password: str
    timeout: float = DEFAULT_TIMEOUT
    delay: float = DEFAULT_DELAY
    verbose: bool = False
    config_path: str = DEFAULT_CONFIG


def load_credentials_file(path: str) -> dict:
    """Read url/username/password from a simple key=value text file.

    Accepts 'url', 'username' and 'password' keys (case-insensitive, with or
    without a NAVIDROME_ prefix), '#'/';' comments and optional surrounding
    quotes. Missing files return an empty dict.
    """
    creds = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(("#", ";")) or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().replace("NAVIDROME_", "").lower()
                if key in ("url", "username", "password"):
                    value = value.strip().strip('"\'')
                    if value:
                        creds[key] = value
    except OSError:
        pass
    return creds


def resolve_credentials(url=None, username=None, password=None,
                        config_path: str = None) -> tuple:
    """Merge credentials: CLI > config file > env > default URL."""
    file_creds = load_credentials_file(config_path) if config_path else {}
    cfg = {
        "url": (url if url is not None
                else file_creds.get("url")
                or os.environ.get("NAVIDROME_URL", "")),
        "username": (username if username is not None
                     else file_creds.get("username")
                     or os.environ.get("NAVIDROME_USER", "")),
        "password": (password if password is not None
                     else file_creds.get("password")
                     or os.environ.get("NAVIDROME_PASSWORD", "")),
    }
    return cfg["url"].rstrip("/"), cfg["username"], cfg["password"]


def add_server_options(parser, *, default_timeout: float = DEFAULT_TIMEOUT,
                       default_delay: float = DEFAULT_DELAY):
    """Add the shared server/credential options to an argparse parser.

    Adds --config, --url, --username, --password, --timeout, --delay,
    --verbose. Call server_from_args() after parse_args() with the same parser
    to resolve the credentials into a ServerConfig object.
    """
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"credentials file with url/username/password "
                             f"(default: {DEFAULT_CONFIG})")
    parser.add_argument("--url", default=None,
                        help="Subsonic URL (overrides config file and NAVIDROME_URL)")
    parser.add_argument("--username", default=None,
                        help="Subsonic username (overrides config file and NAVIDROME_USER)")
    parser.add_argument("--password", default=None,
                        help="Subsonic password (overrides config file and NAVIDROME_PASSWORD)")
    parser.add_argument("--timeout", type=float, default=default_timeout)
    parser.add_argument("--delay", type=float, default=default_delay,
                        help="seconds to wait between API calls")
    parser.add_argument("--verbose", action="store_true",
                        help="print debug output for each request")
    return parser


def server_from_args(parser, args) -> ServerConfig:
    """Resolve the shared options parsed by add_server_options().

    Exits with a usage error (via parser.error) when no username/password are
    available from the CLI, the config file, or the environment.
    """
    base_url, username, password = resolve_credentials(
        args.url, args.username, args.password, args.config)
    if not username and not password:
        parser.error("username/password required (use --username/--password, "
                     "a --config file, or the NAVIDROME_USER/NAVIDROME_PASSWORD env vars)")
    return ServerConfig(
        base_url=base_url, username=username, password=password,
        timeout=args.timeout, delay=args.delay, verbose=args.verbose,
        config_path=args.config,
    )


class AbortSync(Exception):
    """Raised when the user asks to stop processing."""


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = (
        text.replace("\u2018", "'").replace("\u2019", "'")
        .replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u02bc", "'").replace("\u00a0", " ")
    )
    text = text.casefold().strip()
    return " ".join(text.split())


def format_duration(seconds: str) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    return f"{total // 60}:{total % 60:02d}"


def score_song(row: dict, song: dict) -> float:
    """Return a match score for a Subsonic song against a Spotify CSV row."""
    title = normalize(row.get("Track Name", ""))
    artists = row.get("Artist Name(s)", "").strip()
    nav_title = normalize(song.get("title", ""))
    nav_artist = normalize(song.get("artist", ""))

    score = 0.0
    if title and nav_title:
        if title == nav_title:
            score += 40
        elif title in nav_title or nav_title in title:
            score += 20
        else:
            score -= 25

    artist_parts = [p for p in artists.split(";") if p.strip()]
    if artist_parts:
        part_matched = False
        for part in artist_parts:
            a = normalize(part)
            if a and a == nav_artist:
                score += 30
                part_matched = True
            elif a and nav_artist and len(a) >= 3 and (a in nav_artist or nav_artist in a):
                score += 12
                part_matched = True
        if not part_matched:
            score -= 25

    try:
        spotify_ms = int(float(row.get("Duration (ms)", 0) or 0))
    except (TypeError, ValueError):
        spotify_ms = 0
    if spotify_ms:
        try:
            song_ms = int(float(song.get("duration", 0) or 0)) * 1000
        except (TypeError, ValueError):
            song_ms = 0
        diff = abs(song_ms - spotify_ms)
        if diff < 5000:
            score += 15
        elif diff < 30000:
            score += 5
        else:
            score -= 15

    return score


def song_choice_label(song: dict) -> str:
    """Format a Subsonic song as a questionary choice label."""
    label = f"{song.get('title', '?')} — {song.get('artist', '?')}"
    album = song.get("album", "") or ""
    if album:
        label += f" [{album}]"
    dur = format_duration(song.get("duration"))
    if dur:
        label += f" ({dur})"
    label += f"  #id={song.get('id')}"
    return label


def interactive_resolve(question: str, songs: list, search_func, default_query: str,
                        rank=None, max_choices: int = 8):
    """Let the user resolve a track by picking among candidate songs.

    Present the initial songs as selectable choices along with Skip / search
    again / Abort. 'Search with different terms' re-queries via search_func.
    rank(songs) optionally reorders/limits the candidates before they are shown.

    Returns the chosen song (dict) or None if the user chose to skip it.
    Raises AbortSync when the user aborts.
    """
    current = list(songs)
    while True:
        ordered = rank(current) if rank else current
        choices = []
        candidates = [s for s in ordered if s.get("id")]
        if candidates:
            choices.append(questionary.Separator(" Query result "))
            choices.extend({"name": song_choice_label(s), "value": s["id"]}
                           for s in candidates[:max_choices])
            choices.append(questionary.Separator())
        choices.append({"name": "Skip this track", "value": "__skip__"})
        choices.append({"name": "Search with different terms", "value": "__search__"})
        choices.append({"name": "Abort", "value": "__abort__"})
        try:
            choice = questionary.select(question, choices=choices).unsafe_ask()
        except KeyboardInterrupt:
            raise AbortSync() from None
        if choice == "__skip__":
            return None
        if choice == "__abort__":
            raise AbortSync()
        if choice == "__search__":
            new_query = questionary.text(
                "Search terms:", default=default_query,
            ).unsafe_ask()
            query = new_query.strip() or default_query
            current = search_func(query)
            continue
        return next(s for s in current if s.get("id") == choice)