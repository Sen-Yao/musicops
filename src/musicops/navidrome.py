from __future__ import annotations

import hashlib
import os
import random
import string
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUBSONIC_NS = "{http://subsonic.org/restapi}"


@dataclass
class NavidromeConfig:
    url: str = "http://192.168.1.6:4533"
    public_url: str = "https://music.senyao.org"
    user: str = "Senyao"
    password: str = ""
    version: str = "1.16.1"
    client: str = "musicops"

    @classmethod
    def from_env(cls) -> "NavidromeConfig":
        return cls(
            url=os.environ.get("NAVIDROME_URL", "http://192.168.1.6:4533").rstrip("/"),
            public_url=os.environ.get("NAVIDROME_PUBLIC_URL", "https://music.senyao.org").rstrip("/"),
            user=os.environ.get("NAVIDROME_USER", "Senyao"),
            password=os.environ.get("NAVIDROME_PASSWORD", ""),
            version=os.environ.get("NAVIDROME_VERSION", "1.16.1"),
            client=os.environ.get("NAVIDROME_CLIENT", "musicops"),
        )


class NavidromeClient:
    """Read-only Navidrome/Subsonic API helper.

    The client never returns the Navidrome password. It may return a short-lived
    Subsonic-style public cover URL because the existing review-page flow embeds
    such URLs in email previews; callers must avoid logging full URLs if that is
    considered sensitive in their context.
    """

    def __init__(self, config: NavidromeConfig | None = None, opener=None):
        self.config = config or NavidromeConfig.from_env()
        self.opener = opener or urllib.request.urlopen

    def configured(self) -> bool:
        return bool(self.config.password)

    def _auth(self) -> dict[str, str]:
        salt = "".join(random.choices(string.digits, k=6))
        token = hashlib.md5((self.config.password + salt).encode("utf-8")).hexdigest()
        return {"u": self.config.user, "t": token, "s": salt, "v": self.config.version, "c": self.config.client}

    def _request_text(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        if not self.configured():
            raise RuntimeError("navidrome_not_configured")
        query_params = self._auth()
        if params:
            query_params.update({k: str(v) for k, v in params.items() if v is not None})
        query = urllib.parse.urlencode(query_params)
        url = f"{self.config.url}/rest/{endpoint}?{query}"
        with self.opener(urllib.request.Request(url), timeout=15) as response:
            return response.read().decode("utf-8")

    def start_scan(self) -> dict[str, Any]:
        try:
            xml_text = self._request_text("startScan.view")
            root = ET.fromstring(xml_text)
        except Exception as exc:
            return {"ok": False, "error_type": exc.__class__.__name__, "detail": str(exc)}
        status = root.get("status") or ""
        return {"ok": status.lower() == "ok", "status": status}

    def search_songs(self, query: str, count: int = 5) -> list[dict[str, Any]]:
        xml_text = self._request_text("search3.view", {"query": query, "songCount": count})
        root = ET.fromstring(xml_text)
        songs: list[dict[str, Any]] = []
        for song in root.findall(f".//{SUBSONIC_NS}song"):
            songs.append(
                {
                    "id": song.get("id") or "",
                    "title": song.get("title") or "",
                    "artist": song.get("artist") or "",
                    "album": song.get("album") or "",
                    "duration": _safe_int(song.get("duration")),
                    "path": song.get("path") or "",
                    "coverArt": song.get("coverArt") or "",
                    "coverArtId": song.get("coverArt") or "",
                    "albumId": song.get("albumId") or "",
                    "year": song.get("year") or "",
                    "releaseDate": song.get("releaseDate") or "",
                    "date": song.get("date") or "",
                }
            )
        return songs

    def review_cover_evidence(self, title: str, artist: str, count: int = 5) -> dict[str, Any]:
        query = f"{title or ''} {artist or ''}".strip()
        if not query:
            return {"ok": False, "error_type": "empty_query"}
        try:
            songs = self.search_songs(query, count=count)
        except Exception as exc:
            return {"ok": False, "error_type": exc.__class__.__name__, "detail": str(exc)}

        matched = _select_match(songs, title)
        payload: dict[str, Any] = {
            "ok": True,
            "query": query,
            "result_count": len(songs),
            "matched_song": matched,
            "cover": {"present": False},
        }
        if not matched:
            return payload

        cover_ids = _cover_candidates(matched)
        for cover_id in cover_ids:
            cover = self._validated_cover(cover_id)
            if cover.get("present"):
                payload["cover"] = cover
                break
        return payload

    def album_tracks_evidence(self, album: str, album_artist: str = "") -> dict[str, Any]:
        query = f"album:{album or ''} {album_artist or ''}".strip()
        if not (album or "").strip():
            return {"ok": False, "error_type": "empty_album"}
        try:
            # Subsonic search3 is not a Navidrome SQLite view, but it gives a
            # bounded, read-only album track evidence path without exposing DB
            # internals or credentials to music-manager.
            songs = self.search_songs(query, count=200)
        except Exception as exc:
            return {"ok": False, "error_type": exc.__class__.__name__, "detail": str(exc)}

        album_norm = album.strip().casefold()
        artist_norm = album_artist.strip().casefold()
        tracks = []
        for song in songs:
            if album_norm and (song.get("album") or "").strip().casefold() != album_norm:
                continue
            if artist_norm and artist_norm not in (song.get("artist") or "").strip().casefold():
                continue
            path = song.get("path") or ""
            suffix = Path(path).suffix.lower().lstrip(".") if path else ""
            tracks.append(
                {
                    "path": path,
                    "title": song.get("title") or "",
                    "artist": song.get("artist") or "",
                    "album": song.get("album") or "",
                    "album_artist": song.get("albumArtist") or song.get("artist") or "",
                    "album_id": song.get("albumId") or "",
                    "mbz_album_id": song.get("musicBrainzAlbumId") or "",
                    "track_number": _safe_int(song.get("track")),
                    "disc_number": _safe_int(song.get("discNumber")) or 1,
                    "suffix": suffix,
                    "lyrics_present": bool(song.get("lyrics")),
                    "missing": False,
                }
            )
        tracks.sort(key=lambda t: ((t.get("disc_number") or 1), (t.get("track_number") or 9999), t.get("title") or ""))
        return {
            "ok": True,
            "album": album,
            "album_artist": album_artist,
            "track_count": len(tracks),
            "lyrics_missing_count": sum(1 for track in tracks if not track.get("lyrics_present")),
            "tracks": tracks,
        }

    def db_search_evidence(self, query: str, limit: int = 20) -> dict[str, Any]:
        query = (query or "").strip()
        bounded_limit = max(1, min(int(limit), 100))
        if not query:
            return {"ok": False, "error_type": "empty_query", "results": [], "result_count": 0, "limit": bounded_limit}
        try:
            songs = self.search_songs(query, count=bounded_limit)
        except Exception as exc:
            return {"ok": False, "error_type": exc.__class__.__name__, "detail": str(exc), "results": [], "result_count": 0, "limit": bounded_limit}
        return {"ok": True, "query": query, "limit": bounded_limit, "result_count": len(songs), "results": songs}

    def path_evidence(
        self,
        file_path: str,
        query: str = "",
        limit: int = 20,
        expected_metadata: dict[str, Any] | None = None,
        match_strategy: str = "exact_path",
    ) -> dict[str, Any]:
        expected_path = (file_path or "").strip()
        if not expected_path:
            return {"ok": False, "error_type": "empty_path", "file_path": expected_path, "matched": False, "match_type": "", "results": []}
        expected = expected_metadata or {}
        default_query = str(expected.get("title") or expected.get("album") or expected_path) if match_strategy == "unique_metadata" else expected_path
        search_query = (query or default_query).strip()
        evidence = self.db_search_evidence(search_query, limit=limit)
        results = evidence.get("results") or []
        path_matched_rows = [row for row in results if str(row.get("path") or "") == expected_path]
        metadata_candidates = [row for row in results if _metadata_row_matches(row, expected)] if expected else []
        path_matched = bool(path_matched_rows)
        if match_strategy == "unique_metadata":
            matched = len(metadata_candidates) == 1
            matched_rows = metadata_candidates if matched else []
            metadata_matched = matched
            match_type = "unique_metadata" if matched else ""
        else:
            metadata_matched_rows = [row for row in path_matched_rows if _metadata_row_matches(row, expected)] if expected else path_matched_rows
            matched_rows = metadata_matched_rows if expected else path_matched_rows
            metadata_matched = bool(metadata_matched_rows) if expected else None
            matched = path_matched and (metadata_matched if expected else True)
            match_type = "path_and_metadata" if matched and expected else ("path" if matched else "")
        return {
            "ok": bool(evidence.get("ok")),
            "file_path": expected_path,
            "query": search_query,
            "match_strategy": match_strategy,
            "matched": matched,
            "path_matched": path_matched,
            "metadata_matched": metadata_matched,
            "metadata_candidate_count": len(metadata_candidates),
            "metadata_ambiguous": len(metadata_candidates) > 1,
            "match_type": match_type,
            "matched_rows": matched_rows,
            "evidence": evidence,
        }

    def album_group_evidence(self, album: str, album_artist: str = "") -> dict[str, Any]:
        tracks_payload = self.album_tracks_evidence(album=album, album_artist=album_artist)
        if not tracks_payload.get("ok"):
            return tracks_payload
        groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for track in tracks_payload.get("tracks", []):
            key = (
                str(track.get("album") or ""),
                str(track.get("album_artist") or ""),
                str(track.get("album_id") or ""),
                str(track.get("mbz_album_id") or ""),
            )
            group = groups.setdefault(
                key,
                {
                    "album": key[0],
                    "album_artist": key[1],
                    "album_id": key[2],
                    "mbz_album_id": key[3],
                    "track_count": 0,
                    "tracks": [],
                    "paths": [],
                },
            )
            group["track_count"] += 1
            group["tracks"].append(
                {
                    "path": track.get("path") or "",
                    "title": track.get("title") or "",
                    "track_number": track.get("track_number"),
                    "disc_number": track.get("disc_number"),
                }
            )
            if track.get("path"):
                group["paths"].append(track.get("path"))
        group_list = list(groups.values())
        return {
            "ok": True,
            "album": album,
            "album_artist": album_artist,
            "track_count": tracks_payload.get("track_count", 0),
            "split_detected": len(group_list) > 1,
            "group_count": len(group_list),
            "groups": group_list,
        }

    def album_split_evidence(self, album: str) -> dict[str, Any]:
        return self.album_group_evidence(album=album)

    def orphan_evidence(self, query: str = "", limit: int = 100) -> dict[str, Any]:
        # Subsonic API cannot safely expose Navidrome's internal media_file table.
        # Keep this endpoint read-only and conservative: it reports candidate rows
        # whose library path is absent from the MusicOps /music mount when that
        # path can be checked, otherwise it returns an empty diagnostic set.
        search_query = (query or "").strip()
        if not search_query:
            return {"ok": True, "query": search_query, "limit": max(1, min(int(limit), 100)), "orphan_count": 0, "orphans": []}
        found = self.db_search_evidence(search_query, limit=limit)
        if not found.get("ok"):
            return {**found, "orphan_count": 0, "orphans": []}
        orphans = []
        for song in found.get("results", []):
            path = str(song.get("path") or "")
            if not path:
                continue
            library_path = Path("/music") / path
            if not library_path.exists():
                orphans.append({"path": path, "title": song.get("title") or "", "artist": song.get("artist") or "", "reason": "missing_library_file"})
        bounded_limit = max(1, min(int(limit), 100))
        return {"ok": True, "query": search_query, "limit": bounded_limit, "orphan_count": len(orphans), "orphans": orphans[:bounded_limit]}

    def _validated_cover(self, cover_id: str) -> dict[str, Any]:
        auth = self._auth()
        query_params = {"id": cover_id, "size": "300", **auth}
        query = urllib.parse.urlencode(query_params)
        validation_url = f"{self.config.url}/rest/getCoverArt?{query}"
        public_query = urllib.parse.urlencode({"id": cover_id, "size": "300"})
        safe_public_url = f"{self.config.public_url}/rest/getCoverArt?{public_query}"
        try:
            with self.opener(urllib.request.Request(validation_url), timeout=10) as response:
                content_type = response.headers.get("content-type", "")
                if getattr(response, "status", 200) == 200 and content_type.startswith("image/"):
                    return {
                        "present": True,
                        "cover_art_id": cover_id,
                        "content_type": content_type,
                        "public_url": safe_public_url,
                        "public_url_redacted": True,
                    }
        except Exception as exc:
            return {"present": False, "cover_art_id": cover_id, "error_type": exc.__class__.__name__}
        return {"present": False, "cover_art_id": cover_id}


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _select_match(songs: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    title_norm = (title or "").strip().casefold()
    if title_norm:
        for song in songs:
            if title_norm in (song.get("title") or "").casefold():
                return song
    if len(songs) == 1:
        return songs[0]
    return None


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _metadata_row_matches(row: dict[str, Any], expected: dict[str, Any]) -> bool:
    title = _norm_text(expected.get("title"))
    album = _norm_text(expected.get("album"))
    artist = _norm_text(expected.get("artist"))
    album_artist = _norm_text(expected.get("album_artist"))
    if title and _norm_text(row.get("title")) != title:
        return False
    if album and _norm_text(row.get("album")) != album:
        return False
    row_artist = _norm_text(row.get("artist"))
    if artist and artist not in row_artist:
        return False
    if album_artist and album_artist not in row_artist:
        return False
    return bool(title or album or artist or album_artist)


def _cover_candidates(song: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for key in ("coverArt", "coverArtId", "id", "albumId"):
        value = str(song.get(key) or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
