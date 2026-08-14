"""Shared Subsonic/Navidrome REST client.

A single class-based client used by every Subify script. Handles the shared
credential params, request/error handling, track search (optionally restricted
to specific music libraries) and playlist management.
"""

import requests

from cli import ServerConfig

API_VERSION = "1.16.1"


class SubsonicClient:
    def __init__(self, server: ServerConfig, *, client_name: str = "subify"):
        self.server = server
        self.api_version = API_VERSION
        self.client_name = client_name
        self.session = requests.Session()
        self.music_folder_ids: list = []
        self.music_folder_names: list = []

    def _base_params(self) -> dict:
        return {
            "u": self.server.username,
            "p": self.server.password,
            "v": self.api_version,
            "c": self.client_name,
            "f": "json",
        }

    def _request(self, endpoint: str, params: dict, post: bool = False) -> dict:
        url = f"{self.server.base_url}/rest/{endpoint}"
        if self.server.verbose:
            print(f"[verbose] {('POST' if post else 'GET')} /rest/{endpoint} {params}")
        if post:
            resp = self.session.post(
                url,
                params=self._base_params(),
                data=params,
                timeout=self.server.timeout,
            )
        else:
            resp = self.session.get(
                url,
                params={**self._base_params(), **params},
                timeout=self.server.timeout,
            )
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"HTTP {resp.status_code} on /rest/{endpoint}: {resp.text[:300]}"
            ) from exc
        data = resp.json().get("subsonic-response", {})
        if data.get("status") == "failed":
            raise RuntimeError(
                f"Subsonic error on /rest/{endpoint}: {data.get('error', {}).get('message')}"
            )
        return data

    def ping(self) -> dict:
        return self._request("ping", {})

    def search_tracks(
        self, query: str, song_count: int = 10, music_folder_id=None
    ) -> list:
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

    def get_playlists(self) -> list:
        data = self._request("getPlaylists", {})
        return data.get("playlists", {}).get("playlist", [])

    def get_playlist(self, playlist_id: str) -> dict:
        data = self._request("getPlaylist", {"id": playlist_id})
        return data.get("playlist", {})

    def create_playlist(
        self, playlist_id: str = None, name: str = None, song_ids: list = None
    ) -> dict:
        params = {}
        if playlist_id:
            params["playlistId"] = playlist_id
        if name:
            params["name"] = name
        for sid in song_ids or []:
            params.setdefault("songId", []).append(sid)
        data = self._request("createPlaylist", params, post=True)
        return data.get("playlist", {})