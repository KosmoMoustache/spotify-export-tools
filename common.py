"""Subify tooling."""

import sys
import unicodedata

import questionary


class AbortSync(Exception):
    """Raised when the user asks to stop processing."""


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u02bc", "'")
        .replace("\u00a0", " ")
    )
    text = text.casefold().strip()
    return " ".join(text.split())


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
    return any(ap in b or b in ap for ap in a_parts) or bool(
        b_words & {w for p in a_parts for w in p.split()}
    )


def load_cache(path: str) -> dict:
    """Load query -> title|||artist pairs from a txt file."""
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
    """Write query -> title|||artist pairs to a txt file."""
    entries = sorted(mapping.items())
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for key, value in entries:
            f.write(f"{key}\t{value}\n")
    return len(entries)


def resolve_from_cache(client, prefix: str, query: str, cache: dict, search=None):
    """Resolve a query via a previously logged match (query -> title|||artist).

    Searches the server for the cached title/artist and returns the matching
    song, or None when the cache has no usable entry. Logs usage; a cached
    match that no longer exists on the server is dropped. search defaults to
    client.search_tracks; pass the client's library-aware search to honor
    folder restrictions.
    """
    entry = cache.get(query)
    if not entry or "|||" not in entry:
        return None
    nav_title, nav_artist = entry.split("|||", 1)
    find = search or client.search_tracks
    matches = find(f"{nav_title} {nav_artist}".replace(";", " "), song_count=20)
    for song in matches:
        if artist_matches(nav_artist, song.get("artist", "")) and title_matches(
            nav_title, song.get("title", "")
        ):
            print(
                f"{prefix} Using cached match: '{query}' -> '{nav_title} {nav_artist}'"
            )
            return song
    cache.pop(query, None)
    print(
        f"{prefix} Cached match for '{query}' ('{nav_title} {nav_artist}') "
        f"no longer on the server; dropping it",
        file=sys.stderr,
    )
    return None


def log_cached_match(prefix: str, query: str, song: dict, cache: dict):
    """Record a query -> title|||artist link for a manually confirmed match."""
    nav_title = song.get("title", "")
    nav_artist = song.get("artist", "")
    cache[query] = f"{nav_title}|||{nav_artist}"
    print(f"{prefix} Logged match: '{query}' -> '{nav_title} {nav_artist}'")


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
            elif (
                a
                and nav_artist
                and len(a) >= 3
                and (a in nav_artist or nav_artist in a)
            ):
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


def interactive_resolve(
    question: str,
    songs: list,
    search_func,
    default_query: str,
    rank=None,
    max_choices: int = 8,
):
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
            choices.extend(
                {"name": song_choice_label(s), "value": s["id"]}
                for s in candidates[:max_choices]
            )
            choices.append(questionary.Separator())
        choices.append({"name": "Skip this track", "value": "__skip__"})
        choices.append({"name": "Search with different terms", "value": "__search__"})
        choices.append(
            {
                "name": "Quit (save logged matches and exit)",
                "value": "__abort__",
            }
        )
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
                "Search terms:",
                default=default_query,
            ).unsafe_ask()
            query = new_query.strip() or default_query
            current = search_func(query)
            continue
        return next(s for s in current if s.get("id") == choice)
