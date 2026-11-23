from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import threading
import time
from typing import Any

import httpx

from musicops.identity import artist_identity_compatible, identity_key as _identity_key, simplify_chinese


DEFAULT_MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
DEFAULT_COVER_ART_API = "https://coverartarchive.org"
DEFAULT_NETEASE_API = "https://music.163.com/api"
DEFAULT_QQ_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
DEFAULT_QQ_ALBUM_API = "https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg"
DEFAULT_APPLE_MUSIC_API = "https://api.music.apple.com/v1"
DEFAULT_SPOTIFY_API = "https://api.spotify.com/v1"
DEFAULT_SPOTIFY_ACCOUNTS_API = "https://accounts.spotify.com/api"
SUPPORTED_PROVIDERS = ("musicbrainz", "netease", "qq", "apple_music", "spotify")
SUPPLEMENTAL_FIELDS = {"title", "artist", "album", "album_artist", "date", "cover"}

def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _csv(value: str | None, default: str) -> list[str]:
    rows = [item.strip() for item in str(value or default).split(",") if item.strip()]
    return list(dict.fromkeys(rows))


def _artist_credit(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        artist = item.get("artist") or {}
        name = str(item.get("name") or artist.get("name") or "").strip()
        if name:
            parts.append(name)
        joinphrase = str(item.get("joinphrase") or "")
        if joinphrase and parts:
            parts[-1] += joinphrase
    return "".join(parts).strip()


def _joined_artists(value: Any) -> str:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return ""
    names = []
    for item in value:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    return "; ".join(names)


def _positive_int(value: Any, default: int = 0) -> int:
    try:
        number = int(str(value or "").strip())
        return number if number > 0 else default
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value or ""))
        return int(match.group(0)) if match and int(match.group(0)) > 0 else default


def _date_from_millis(value: Any) -> str:
    try:
        timestamp = float(value or 0) / 1000
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat() if timestamp > 0 else ""


def _cover_media_type(data: bytes, header: str) -> str:
    value = str(header or "").split(";", 1)[0].strip().lower()
    if value in {"image/jpeg", "image/png"}:
        return value
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return ""


def _missing_manifest_fields(
    *,
    provider_release_id: str,
    album: str,
    album_artist: str,
    date: str,
    tracks: list[dict[str, Any]],
    essential_only: bool = True,
) -> list[str]:
    release = {
        "provider_release_id": provider_release_id,
        "album": album,
        "album_artist": album_artist,
        "date": date,
    }
    missing = [key for key, value in release.items() if value in (None, "", 0) and (not essential_only or key == "provider_release_id")]
    required_track_fields = {
        "provider_track_id", "title", "artist", "album", "album_artist", "date",
        "track_number", "track_total", "disc_number", "disc_total",
    }
    if essential_only:
        required_track_fields = {"provider_track_id", "title", "artist"}
    for index, track in enumerate(tracks):
        for field in sorted(required_track_fields):
            if track.get(field) in (None, "", 0):
                missing.append(f"tracks[{index}].{field}")
    if not tracks:
        missing.append("tracks")
    return sorted(set(missing))


class MetadataProviderClient:
    """Aggregate canonical metadata providers without silently merging entities."""

    def __init__(
        self,
        *,
        provider: str | None = None,
        providers: list[str] | tuple[str, ...] | None = None,
        api_url: str | None = None,
        cover_art_url: str | None = None,
        netease_api_url: str | None = None,
        qq_api_url: str | None = None,
        qq_album_api_url: str | None = None,
        apple_music_api_url: str | None = None,
        apple_music_token: str | None = None,
        apple_music_storefronts: list[str] | tuple[str, ...] | None = None,
        spotify_api_url: str | None = None,
        spotify_accounts_api_url: str | None = None,
        spotify_client_id: str | None = None,
        spotify_client_secret: str | None = None,
        spotify_markets: list[str] | tuple[str, ...] | None = None,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ):
        if providers is not None:
            configured_providers = [str(item).strip().lower() for item in providers if str(item).strip()]
        else:
            configured = os.environ.get("MUSICOPS_METADATA_PROVIDERS")
            legacy = provider or os.environ.get("MUSICOPS_METADATA_PROVIDER") or "musicbrainz"
            configured_providers = _csv(configured, legacy)
        unknown = sorted(set(configured_providers) - set(SUPPORTED_PROVIDERS))
        if unknown:
            raise ValueError(f"unsupported_metadata_providers:{','.join(unknown)}")
        self.providers = list(dict.fromkeys(configured_providers))
        self.provider = self.providers[0] if self.providers else ""
        self.api_url = (api_url or os.environ.get("MUSICOPS_MUSICBRAINZ_API_URL") or DEFAULT_MUSICBRAINZ_API).rstrip("/")
        self.cover_art_url = (cover_art_url or os.environ.get("MUSICOPS_COVER_ART_API_URL") or DEFAULT_COVER_ART_API).rstrip("/")
        self.netease_api_url = (netease_api_url or os.environ.get("MUSICOPS_NETEASE_API_URL") or DEFAULT_NETEASE_API).rstrip("/")
        self.qq_api_url = (qq_api_url or os.environ.get("MUSICOPS_QQ_API_URL") or DEFAULT_QQ_API).rstrip("/")
        self.qq_album_api_url = (qq_album_api_url or os.environ.get("MUSICOPS_QQ_ALBUM_API_URL") or DEFAULT_QQ_ALBUM_API).rstrip("/")
        self.apple_music_api_url = (apple_music_api_url or os.environ.get("MUSICOPS_APPLE_MUSIC_API_URL") or DEFAULT_APPLE_MUSIC_API).rstrip("/")
        self.apple_music_token = apple_music_token if apple_music_token is not None else os.environ.get("MUSICOPS_APPLE_MUSIC_DEVELOPER_TOKEN", "")
        self.apple_music_storefronts = list(apple_music_storefronts or _csv(os.environ.get("MUSICOPS_APPLE_MUSIC_STOREFRONTS"), "cn,tw,hk,us"))
        self.spotify_api_url = (spotify_api_url or os.environ.get("MUSICOPS_SPOTIFY_API_URL") or DEFAULT_SPOTIFY_API).rstrip("/")
        self.spotify_accounts_api_url = (
            spotify_accounts_api_url
            or os.environ.get("MUSICOPS_SPOTIFY_ACCOUNTS_API_URL")
            or DEFAULT_SPOTIFY_ACCOUNTS_API
        ).rstrip("/")
        self.spotify_client_id = spotify_client_id if spotify_client_id is not None else os.environ.get("MUSICOPS_SPOTIFY_CLIENT_ID", "")
        self.spotify_client_secret = spotify_client_secret if spotify_client_secret is not None else os.environ.get("MUSICOPS_SPOTIFY_CLIENT_SECRET", "")
        self.spotify_markets = [str(item).upper() for item in (spotify_markets or _csv(os.environ.get("MUSICOPS_SPOTIFY_MARKETS"), "CN,TW,HK,US"))]
        self.timeout = timeout
        self.client = client
        self.user_agent = os.environ.get("MUSICOPS_METADATA_USER_AGENT", "musicops/0.1 (personal library manager)")
        self._spotify_token = ""
        self._spotify_token_expires_at = 0.0
        self._spotify_token_lock = threading.Lock()

    def _provider_configured(self, provider: str) -> bool:
        if provider == "musicbrainz":
            return bool(self.api_url)
        if provider == "netease":
            return bool(self.netease_api_url)
        if provider == "qq":
            return bool(self.qq_api_url and self.qq_album_api_url)
        if provider == "apple_music":
            return bool(self.apple_music_api_url and self.apple_music_token and self.apple_music_storefronts)
        if provider == "spotify":
            return bool(self.spotify_api_url and self.spotify_client_id and self.spotify_client_secret and self.spotify_markets)
        return False

    def configured(self) -> bool:
        return any(self._provider_configured(provider) for provider in self.providers)

    def available_providers(self) -> list[str]:
        return [provider for provider in self.providers if self._provider_configured(provider)]

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json", **dict(kwargs.pop("headers", {}) or {})}
        if self.client is not None:
            response = self.client.request(method, url, headers=headers, **kwargs)
        else:
            response = httpx.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
                follow_redirects=True,
                **kwargs,
            )
        response.raise_for_status()
        return response

    def search(self, *, query: str, artist: str = "", album: str = "", limit: int = 10) -> dict[str, Any]:
        available = self.available_providers()
        warnings = [
            {"code": "provider_not_configured", "provider": provider}
            for provider in self.providers
            if provider not in available
        ]
        if not available:
            return {
                "ok": False,
                "error": "metadata_provider_not_configured",
                "providers": [],
                "candidates": [],
                "warnings": warnings,
            }

        candidates: list[dict[str, Any]] = []
        succeeded: list[str] = []
        with ThreadPoolExecutor(max_workers=min(len(available), 5)) as executor:
            futures = {
                executor.submit(self._search_provider, provider, query=query, artist=artist, album=album, limit=limit): provider
                for provider in available
            }
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    candidates.extend(future.result())
                    succeeded.append(provider)
                except Exception as exc:
                    warnings.append({"code": "provider_search_failed", "provider": provider, "detail": exc.__class__.__name__})

        provider_order = {provider: index for index, provider in enumerate(self.providers)}
        candidates.sort(key=lambda row: provider_order.get(str(row.get("provider") or ""), 999))
        return {
            "ok": bool(succeeded),
            "query": {"query": query, "artist": artist, "album": album},
            "providers": [provider for provider in self.providers if provider in succeeded],
            "result_count": len(candidates),
            "candidates": candidates,
            "warnings": warnings,
            "source_sha256": _sha256_json(candidates),
            "fetched_at": time.time(),
        }

    def _search_provider(self, provider: str, *, query: str, artist: str, album: str, limit: int) -> list[dict[str, Any]]:
        method = getattr(self, f"_search_{provider}")
        return list(method(query=query, artist=artist, album=album, limit=limit))

    def _search_musicbrainz(self, *, query: str, artist: str, album: str, limit: int) -> list[dict[str, Any]]:
        release_term = str(album or query).replace('"', "").strip()
        artist_term = str(artist or "").replace('"', "").strip()
        terms = [f'{"release" if album.strip() else "recording"}:"{release_term}"']
        if artist_term:
            terms.append(f'artist:"{artist_term}"')
        payload = self._request(
            "GET",
            f"{self.api_url}/release/",
            params={"query": " AND ".join(terms), "fmt": "json", "limit": limit},
        ).json()
        rows = []
        for release in list(payload.get("releases") or [])[:limit]:
            release_group = release.get("release-group") or {}
            rows.append({
                "provider": "musicbrainz",
                "provider_release_id": str(release.get("id") or ""),
                "title": str(release.get("title") or ""),
                "album_artist": _artist_credit(release.get("artist-credit")),
                "date": str(release.get("date") or ""),
                "country": str(release.get("country") or ""),
                "market": str(release.get("country") or "").lower(),
                "track_count": int(release.get("track-count") or 0),
                "release_group_id": str(release_group.get("id") or ""),
                "primary_type": str(release_group.get("primary-type") or ""),
                "secondary_types": list(release_group.get("secondary-types") or []),
                "upc": str(release.get("barcode") or ""),
            })
        return rows

    def _search_netease(self, *, query: str, artist: str, album: str, limit: int) -> list[dict[str, Any]]:
        term = " ".join(part for part in (simplify_chinese(album or query), simplify_chinese(artist)) if part)
        payload = self._request(
            "GET",
            f"{self.netease_api_url}/search/get/web",
            params={"s": term, "type": 10, "offset": 0, "limit": limit},
            headers={"User-Agent": "musicops/2"},
        ).json()
        rows = []
        for album_row in list((payload.get("result") or {}).get("albums") or [])[:limit]:
            artists = album_row.get("artists") or album_row.get("artist") or []
            rows.append({
                "provider": "netease",
                "provider_release_id": str(album_row.get("id") or ""),
                "title": str(album_row.get("name") or ""),
                "album_artist": _joined_artists(artists),
                "date": _date_from_millis(album_row.get("publishTime")),
                "country": "CN",
                "market": "cn",
                "track_count": _positive_int(album_row.get("size")),
                "release_group_id": "",
                "primary_type": str(album_row.get("type") or "Album"),
                "secondary_types": [],
                "upc": "",
            })
        return rows

    def _search_qq(self, *, query: str, artist: str, album: str, limit: int) -> list[dict[str, Any]]:
        body = {
            "comm": {"ct": "6", "cv": "80600"},
            "music.search.SearchCgiService.DoSearchForQQMusicDesktop": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {
                    "query": " ".join(part for part in (simplify_chinese(album or query), simplify_chinese(artist)) if part),
                    "num_per_page": min(max(limit * 3, limit), 50),
                    "page_num": 1,
                },
            },
        }
        payload = self._request(
            "POST",
            self.qq_api_url,
            json=body,
            headers={"Referer": "https://y.qq.com", "User-Agent": "musicops/2"},
        ).json()
        songs = (((payload.get("music.search.SearchCgiService.DoSearchForQQMusicDesktop") or {}).get("data") or {}).get("body") or {}).get("song") or {}
        rows = []
        seen: set[str] = set()
        for song in songs.get("list") or []:
            album_row = song.get("album") or {}
            album_id = str(album_row.get("mid") or "")
            if not album_id or album_id in seen:
                continue
            seen.add(album_id)
            rows.append({
                "provider": "qq",
                "provider_release_id": album_id,
                "title": str(album_row.get("name") or song.get("albumname") or ""),
                "album_artist": _joined_artists(song.get("singer") or []),
                "date": str(song.get("time_public") or ""),
                "country": "CN",
                "market": "cn",
                "track_count": _positive_int(album_row.get("total_song_num")),
                "release_group_id": "",
                "primary_type": "Album",
                "secondary_types": [],
                "upc": "",
            })
            if len(rows) >= limit:
                break
        return rows

    def _apple_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.apple_music_token}"}

    def _search_apple_music(self, *, query: str, artist: str, album: str, limit: int) -> list[dict[str, Any]]:
        term = " ".join(part for part in (simplify_chinese(album or query), simplify_chinese(artist)) if part)
        for storefront in self.apple_music_storefronts:
            payload = self._request(
                "GET",
                f"{self.apple_music_api_url}/catalog/{storefront}/search",
                params={"term": term, "types": "albums", "limit": min(limit, 25)},
                headers=self._apple_headers(),
            ).json()
            albums = (((payload.get("results") or {}).get("albums") or {}).get("data") or [])
            if not albums:
                continue
            rows = []
            for album_row in albums[:limit]:
                attributes = album_row.get("attributes") or {}
                rows.append({
                    "provider": "apple_music",
                    "provider_release_id": str(album_row.get("id") or ""),
                    "title": str(attributes.get("name") or ""),
                    "album_artist": str(attributes.get("artistName") or ""),
                    "date": str(attributes.get("releaseDate") or ""),
                    "country": storefront.upper(),
                    "market": storefront.lower(),
                    "track_count": _positive_int(attributes.get("trackCount")),
                    "release_group_id": "",
                    "primary_type": "Single" if attributes.get("isSingle") else "Album",
                    "secondary_types": [],
                    "upc": str(attributes.get("upc") or ""),
                })
            return rows
        return []

    def _spotify_access_token(self) -> str:
        with self._spotify_token_lock:
            if self._spotify_token and self._spotify_token_expires_at > time.time() + 30:
                return self._spotify_token
            payload = self._request(
                "POST",
                f"{self.spotify_accounts_api_url}/token",
                data={"grant_type": "client_credentials"},
                auth=(self.spotify_client_id, self.spotify_client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ).json()
            token = str(payload.get("access_token") or "")
            if not token:
                raise RuntimeError("spotify_access_token_missing")
            self._spotify_token = token
            self._spotify_token_expires_at = time.time() + int(payload.get("expires_in") or 3600)
            return token

    def _spotify_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._spotify_access_token()}"}

    def _search_spotify(self, *, query: str, artist: str, album: str, limit: int) -> list[dict[str, Any]]:
        terms = []
        if album:
            terms.append(f'album:"{simplify_chinese(album)}"')
        else:
            terms.append(simplify_chinese(query))
        if artist:
            terms.append(f'artist:"{simplify_chinese(artist)}"')
        for market in self.spotify_markets:
            try:
                payload = self._request(
                    "GET",
                    f"{self.spotify_api_url}/search",
                    params={"q": " ".join(terms), "type": "album", "market": market, "limit": min(limit, 50)},
                    headers=self._spotify_headers(),
                ).json()
            except Exception:
                continue
            albums = ((payload.get("albums") or {}).get("items") or [])
            if not albums:
                continue
            rows = []
            for album_row in albums[:limit]:
                rows.append({
                    "provider": "spotify",
                    "provider_release_id": str(album_row.get("id") or ""),
                    "title": str(album_row.get("name") or ""),
                    "album_artist": _joined_artists(album_row.get("artists") or []),
                    "date": str(album_row.get("release_date") or ""),
                    "country": market,
                    "market": market.lower(),
                    "track_count": _positive_int(album_row.get("total_tracks")),
                    "release_group_id": "",
                    "primary_type": str(album_row.get("album_type") or "Album").title(),
                    "secondary_types": [],
                    "upc": str((album_row.get("external_ids") or {}).get("upc") or ""),
                })
            return rows
        return []

    def fetch(
        self,
        *,
        provider: str,
        provider_release_id: str,
        market: str = "",
        supplemental_sources: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if provider not in SUPPORTED_PROVIDERS or not self._provider_configured(provider):
            return {"ok": False, "error": "metadata_provider_not_configured", "provider": provider}
        primary = self._fetch_one(provider, provider_release_id, market=market)
        if not primary.get("ok"):
            return primary

        manifest = json.loads(json.dumps(primary["manifest"]))
        cover = dict(primary.get("cover") or {})
        sources = [{"provider": provider, "provider_release_id": provider_release_id, "market": market}]
        warnings: list[dict[str, Any]] = list(primary.get("warnings") or [])
        for source in supplemental_sources or []:
            supplemental_provider = str(source.get("provider") or "")
            supplemental_id = str(source.get("provider_release_id") or "")
            supplemental_market = str(source.get("market") or "")
            fields = list(dict.fromkeys(str(field) for field in (source.get("fields") or [])))
            if not fields or set(fields) - SUPPLEMENTAL_FIELDS:
                return {"ok": False, "error": "metadata_supplemental_fields_invalid", "fields": fields}
            if not self._provider_configured(supplemental_provider):
                warnings.append({"code": "supplemental_source_skipped", "provider": supplemental_provider, "detail": "provider_not_configured"})
                continue
            try:
                supplemental = self._fetch_one(supplemental_provider, supplemental_id, market=supplemental_market)
            except Exception:
                warnings.append({"code": "supplemental_source_skipped", "provider": supplemental_provider, "detail": "fetch_exception"})
                continue
            if not supplemental.get("ok"):
                warnings.append({"code": "supplemental_source_skipped", "provider": supplemental_provider, "detail": "fetch_failed"})
                continue
            compatible, reason = self._manifests_compatible(manifest, supplemental["manifest"])
            # Merging uses positions, so require them even when a matching UPC exists.
            primary_positions = self._tracks_by_position(manifest)
            supplemental_positions = self._tracks_by_position(supplemental["manifest"])
            positions_match = (
                set(primary_positions) == set(supplemental_positions)
                and len(primary_positions) == len(manifest.get("tracks") or [])
                and len(supplemental_positions) == len(supplemental["manifest"].get("tracks") or [])
                and all(disc > 0 and track > 0 for disc, track in primary_positions)
            )
            if not compatible or not positions_match:
                warnings.append({"code": "supplemental_source_skipped", "provider": supplemental_provider, "detail": reason if not compatible else "track_positions_mismatch"})
                continue
            manifest = self._apply_supplemental(manifest, supplemental["manifest"], fields)
            if "cover" in fields:
                if str((supplemental.get("cover") or {}).get("status") or "") == "available":
                    cover = dict(supplemental["cover"])
                else:
                    warnings.append({"code": "supplemental_cover_unavailable", "provider": supplemental_provider})
            sources.append({
                "provider": supplemental_provider,
                "provider_release_id": supplemental_id,
                "market": supplemental_market,
            })

        manifest = self._simplified_manifest(manifest)
        manifest["provider"] = provider if len(sources) == 1 else "mixed"
        manifest["sources"] = sources
        manifest["source"] = {
            "providers": list(dict.fromkeys(str(source["provider"]) for source in sources)),
            "primary": {"provider": provider, "provider_release_id": provider_release_id, "market": market},
        }
        manifest["cover"] = {key: value for key, value in cover.items() if key not in {"cover_base64", "reason"}}
        missing = _missing_manifest_fields(
            provider_release_id=str(manifest.get("provider_release_id") or ""),
            album=str(manifest.get("album") or ""),
            album_artist=str(manifest.get("album_artist") or ""),
            date=str(manifest.get("date") or ""),
            tracks=list(manifest.get("tracks") or []),
        )
        if missing:
            return {
                "ok": False,
                "error": "canonical_manifest_incomplete",
                "provider": manifest["provider"],
                "provider_release_id": provider_release_id,
                "missing_fields": missing,
            }
        optional_missing = _missing_manifest_fields(
            provider_release_id=provider_release_id, album=str(manifest.get("album") or ""),
            album_artist=str(manifest.get("album_artist") or ""), date=str(manifest.get("date") or ""),
            tracks=list(manifest.get("tracks") or []), essential_only=False,
        )
        if optional_missing:
            warnings.append({"code": "optional_metadata_missing", "fields": optional_missing})
        return {
            "ok": True,
            "provider": manifest["provider"],
            "provider_release_id": provider_release_id,
            "sources": sources,
            "manifest": manifest,
            "manifest_sha256": _sha256_json(manifest),
            "cover": cover,
            "warnings": warnings,
            "fetched_at": time.time(),
        }

    def _fetch_one(self, provider: str, provider_release_id: str, *, market: str = "") -> dict[str, Any]:
        method = getattr(self, f"_fetch_{provider}")
        return dict(method(provider_release_id=provider_release_id, market=market))

    def _fetch_musicbrainz(self, *, provider_release_id: str, market: str = "") -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"{self.api_url}/release/{provider_release_id}",
            params={"inc": "artist-credits+recordings+media+release-groups+isrcs", "fmt": "json"},
        ).json()
        if str(payload.get("id") or "") != provider_release_id:
            return {"ok": False, "error": "metadata_release_identity_mismatch"}
        album = str(payload.get("title") or "").strip()
        album_artist = _artist_credit(payload.get("artist-credit"))
        date = str(payload.get("date") or "").strip()
        media = list(payload.get("media") or [])
        tracks: list[dict[str, Any]] = []
        disc_total = len(media)
        for disc_index, medium in enumerate(media, start=1):
            medium_tracks = list(medium.get("tracks") or [])
            track_total = int(medium.get("track-count") or len(medium_tracks))
            disc_number = int(medium.get("position") or disc_index)
            for fallback_position, row in enumerate(medium_tracks, start=1):
                recording = row.get("recording") or {}
                track = {
                    "provider_track_id": str(row.get("id") or ""),
                    "provider_recording_id": str(recording.get("id") or ""),
                    "title": str(row.get("title") or recording.get("title") or "").strip(),
                    "artist": _artist_credit(row.get("artist-credit") or recording.get("artist-credit")) or album_artist,
                    "album": album,
                    "album_artist": album_artist,
                    "date": date,
                    "track_number": int(row.get("position") or fallback_position),
                    "track_total": track_total,
                    "disc_number": disc_number,
                    "disc_total": disc_total,
                }
                isrcs = list(recording.get("isrcs") or [])
                if isrcs:
                    track["external_ids"] = {"isrc": str(isrcs[0])}
                tracks.append(track)
        missing = _missing_manifest_fields(
            provider_release_id=provider_release_id,
            album=album,
            album_artist=album_artist,
            date=date,
            tracks=tracks,
        )
        if missing:
            return {
                "ok": False,
                "error": "canonical_manifest_incomplete",
                "provider": "musicbrainz",
                "provider_release_id": provider_release_id,
                "missing_fields": missing,
                "source_sha256": _sha256_json(payload),
            }
        cover = self._fetch_image(
            f"{self.cover_art_url}/release/{provider_release_id}/front-500",
            provider="cover_art_archive",
        )
        manifest = {
            "provider": "musicbrainz",
            "provider_release_id": provider_release_id,
            "release_group_id": str((payload.get("release-group") or {}).get("id") or ""),
            "album": album,
            "album_artist": album_artist,
            "date": date,
            "disc_total": disc_total,
            "track_count": len(tracks),
            "tracks": tracks,
            "cover": {key: value for key, value in cover.items() if key not in {"cover_base64", "reason"}},
            "source": {
                "provider": "musicbrainz",
                "provider_release_id": provider_release_id,
                "source_sha256": _sha256_json(payload),
            },
        }
        barcode = str(payload.get("barcode") or "")
        if barcode:
            manifest["external_ids"] = {"upc": barcode}
        return {"ok": True, "provider": "musicbrainz", "manifest": manifest, "cover": cover}

    def _fetch_netease(self, *, provider_release_id: str, market: str = "") -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"{self.netease_api_url}/v1/album/{provider_release_id}",
            headers={"Referer": "https://music.163.com/", "User-Agent": "Mozilla/5.0"},
        ).json()
        album_row = payload.get("album") or {}
        if str(album_row.get("id") or provider_release_id) != provider_release_id:
            return {"ok": False, "error": "metadata_release_identity_mismatch"}
        songs = list(payload.get("songs") or album_row.get("songs") or [])
        album = str(album_row.get("name") or "").strip()
        album_artist = _joined_artists(album_row.get("artists") or album_row.get("artist") or [])
        date = _date_from_millis(album_row.get("publishTime"))
        return self._provider_album_result(
            provider="netease",
            provider_release_id=provider_release_id,
            market=market or "cn",
            album=album,
            album_artist=album_artist,
            date=date,
            raw_tracks=songs,
            track_parser=self._netease_track,
            cover_url=str(album_row.get("picUrl") or album_row.get("blurPicUrl") or ""),
            source_payload=payload,
        )

    def _netease_track(self, row: dict[str, Any], index: int) -> dict[str, Any]:
        return {
            "provider_track_id": str(row.get("id") or ""),
            "provider_recording_id": str(row.get("id") or ""),
            "title": str(row.get("name") or "").strip(),
            "artist": _joined_artists(row.get("artists") or row.get("ar") or []),
            "track_number": _positive_int(row.get("position") or row.get("no"), index),
            "disc_number": _positive_int(row.get("disc") or row.get("cd"), 1),
        }

    def _fetch_qq(self, *, provider_release_id: str, market: str = "") -> dict[str, Any]:
        payload = self._request(
            "GET",
            self.qq_album_api_url,
            params={"albummid": provider_release_id, "format": "json", "outCharset": "utf-8", "platform": "yqq.json"},
            headers={"Referer": "https://y.qq.com", "User-Agent": "musicops/2"},
        ).json()
        album_row = payload.get("data") or {}
        songs = list(album_row.get("list") or [])
        album = str(album_row.get("name") or album_row.get("album_name") or "").strip()
        album_artist = str(album_row.get("singername") or _joined_artists(album_row.get("singer") or [])).strip()
        date = str(album_row.get("aDate") or album_row.get("public_time") or "").strip()
        return self._provider_album_result(
            provider="qq",
            provider_release_id=provider_release_id,
            market=market or "cn",
            album=album,
            album_artist=album_artist,
            date=date,
            raw_tracks=songs,
            track_parser=self._qq_track,
            cover_url=f"https://y.gtimg.cn/music/photo_new/T002R500x500M000{provider_release_id}.jpg",
            source_payload=payload,
        )

    def _qq_track(self, row: dict[str, Any], index: int) -> dict[str, Any]:
        return {
            "provider_track_id": str(row.get("songmid") or row.get("mid") or row.get("songid") or ""),
            "provider_recording_id": str(row.get("songmid") or row.get("mid") or ""),
            "title": str(row.get("songname") or row.get("name") or "").strip(),
            "artist": _joined_artists(row.get("singer") or []),
            "track_number": _positive_int(row.get("index_album") or row.get("track_num"), index),
            "disc_number": _positive_int(row.get("index_cd") or row.get("disc"), 1),
        }

    def _fetch_apple_music(self, *, provider_release_id: str, market: str = "") -> dict[str, Any]:
        storefront = (market or self.apple_music_storefronts[0]).lower()
        payload = self._request(
            "GET",
            f"{self.apple_music_api_url}/catalog/{storefront}/albums/{provider_release_id}",
            params={"include": "tracks"},
            headers=self._apple_headers(),
        ).json()
        rows = list(payload.get("data") or [])
        if len(rows) != 1 or str(rows[0].get("id") or "") != provider_release_id:
            return {"ok": False, "error": "metadata_release_identity_mismatch"}
        album_row = rows[0]
        attributes = album_row.get("attributes") or {}
        track_relationship = (album_row.get("relationships") or {}).get("tracks") or {}
        tracks = list(track_relationship.get("data") or [])
        next_url = str(track_relationship.get("next") or "")
        while next_url:
            page = self._request("GET", next_url, headers=self._apple_headers()).json()
            tracks.extend(page.get("data") or [])
            next_url = str(page.get("next") or "")
        artwork = attributes.get("artwork") or {}
        artwork_url = str(artwork.get("url") or "").replace("{w}", "1000").replace("{h}", "1000")
        result = self._provider_album_result(
            provider="apple_music",
            provider_release_id=provider_release_id,
            market=storefront,
            album=str(attributes.get("name") or ""),
            album_artist=str(attributes.get("artistName") or ""),
            date=str(attributes.get("releaseDate") or ""),
            raw_tracks=tracks,
            track_parser=self._apple_track,
            cover_url=artwork_url,
            source_payload=payload,
        )
        upc = str(attributes.get("upc") or "")
        if result.get("ok") and upc:
            result["manifest"]["external_ids"] = {"upc": upc}
        return result

    def _apple_track(self, row: dict[str, Any], index: int) -> dict[str, Any]:
        attributes = row.get("attributes") or {}
        track = {
            "provider_track_id": str(row.get("id") or ""),
            "provider_recording_id": str(row.get("id") or ""),
            "title": str(attributes.get("name") or "").strip(),
            "artist": str(attributes.get("artistName") or "").strip(),
            "track_number": _positive_int(attributes.get("trackNumber"), index),
            "disc_number": _positive_int(attributes.get("discNumber"), 1),
        }
        isrc = str(attributes.get("isrc") or "")
        if isrc:
            track["external_ids"] = {"isrc": isrc}
        return track

    def _fetch_spotify(self, *, provider_release_id: str, market: str = "") -> dict[str, Any]:
        selected_market = (market or self.spotify_markets[0]).upper()
        payload = self._request(
            "GET",
            f"{self.spotify_api_url}/albums/{provider_release_id}",
            params={"market": selected_market},
            headers=self._spotify_headers(),
        ).json()
        if str(payload.get("id") or "") != provider_release_id:
            return {"ok": False, "error": "metadata_release_identity_mismatch"}
        tracks_payload = payload.get("tracks") or {}
        tracks = list(tracks_payload.get("items") or [])
        next_url = str(tracks_payload.get("next") or "")
        while next_url:
            page = self._request("GET", next_url, headers=self._spotify_headers()).json()
            tracks.extend(page.get("items") or [])
            next_url = str(page.get("next") or "")
        images = list(payload.get("images") or [])
        result = self._provider_album_result(
            provider="spotify",
            provider_release_id=provider_release_id,
            market=selected_market.lower(),
            album=str(payload.get("name") or ""),
            album_artist=_joined_artists(payload.get("artists") or []),
            date=str(payload.get("release_date") or ""),
            raw_tracks=tracks,
            track_parser=self._spotify_track,
            cover_url=str((images[0] if images else {}).get("url") or ""),
            source_payload=payload,
        )
        upc = str((payload.get("external_ids") or {}).get("upc") or "")
        if result.get("ok") and upc:
            result["manifest"]["external_ids"] = {"upc": upc}
        return result

    def _spotify_track(self, row: dict[str, Any], index: int) -> dict[str, Any]:
        track = {
            "provider_track_id": str(row.get("id") or ""),
            "provider_recording_id": str(row.get("id") or ""),
            "title": str(row.get("name") or "").strip(),
            "artist": _joined_artists(row.get("artists") or []),
            "track_number": _positive_int(row.get("track_number"), index),
            "disc_number": _positive_int(row.get("disc_number"), 1),
        }
        isrc = str((row.get("external_ids") or {}).get("isrc") or "")
        if isrc:
            track["external_ids"] = {"isrc": isrc}
        return track

    def _provider_album_result(
        self,
        *,
        provider: str,
        provider_release_id: str,
        market: str,
        album: str,
        album_artist: str,
        date: str,
        raw_tracks: list[dict[str, Any]],
        track_parser: Any,
        cover_url: str,
        source_payload: dict[str, Any],
    ) -> dict[str, Any]:
        parsed = [track_parser(row, index) for index, row in enumerate(raw_tracks, start=1)]
        disc_total = max((_positive_int(track.get("disc_number"), 1) for track in parsed), default=1)
        totals = {
            disc: sum(1 for track in parsed if _positive_int(track.get("disc_number"), 1) == disc)
            for disc in range(1, disc_total + 1)
        }
        tracks = []
        for track in parsed:
            disc_number = _positive_int(track.get("disc_number"), 1)
            track.update({
                "album": album,
                "album_artist": album_artist,
                "date": date,
                "track_total": totals.get(disc_number, len(parsed)),
                "disc_number": disc_number,
                "disc_total": disc_total,
            })
            tracks.append(track)
        missing = _missing_manifest_fields(
            provider_release_id=provider_release_id,
            album=album,
            album_artist=album_artist,
            date=date,
            tracks=tracks,
        )
        if missing:
            return {
                "ok": False,
                "error": "canonical_manifest_incomplete",
                "provider": provider,
                "provider_release_id": provider_release_id,
                "missing_fields": missing,
                "source_sha256": _sha256_json(source_payload),
            }
        cover = self._fetch_image(cover_url, provider=provider) if cover_url else self._unavailable_cover(provider, cover_url, "missing_url")
        manifest = {
            "provider": provider,
            "provider_release_id": provider_release_id,
            "release_group_id": "",
            "album": album,
            "album_artist": album_artist,
            "date": date,
            "disc_total": disc_total,
            "track_count": len(tracks),
            "tracks": tracks,
            "cover": {key: value for key, value in cover.items() if key not in {"cover_base64", "reason"}},
            "source": {
                "provider": provider,
                "provider_release_id": provider_release_id,
                "market": market,
                "source_sha256": _sha256_json(source_payload),
            },
        }
        return {"ok": True, "provider": provider, "manifest": manifest, "cover": cover}

    def _fetch_image(self, url: str, *, provider: str) -> dict[str, Any]:
        try:
            response = self._request("GET", url, headers={"Accept": "image/jpeg,image/png"})
            data = bytes(response.content)
            media_type = _cover_media_type(data, response.headers.get("content-type", ""))
            if not data or len(data) > 20_000_000 or not media_type:
                raise ValueError("cover_payload_invalid")
            return {
                "status": "available",
                "provider": provider,
                "source_url": url,
                "media_type": media_type,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "cover_base64": base64.b64encode(data).decode("ascii"),
            }
        except Exception as exc:
            return self._unavailable_cover(provider, url, f"{exc.__class__.__name__}:{exc}")

    @staticmethod
    def _unavailable_cover(provider: str, url: str, reason: str) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "provider": provider,
            "source_url": url,
            "reason": reason,
            "sha256": "",
            "size": 0,
            "media_type": "",
        }

    @staticmethod
    def _tracks_by_position(manifest: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
        return {
            (_positive_int(track.get("disc_number"), 1), _positive_int(track.get("track_number"), 0)): track
            for track in list(manifest.get("tracks") or [])
        }

    def _manifests_compatible(self, primary: dict[str, Any], supplemental: dict[str, Any]) -> tuple[bool, str]:
        if int(primary.get("track_count") or 0) != int(supplemental.get("track_count") or 0):
            return False, "track_count_mismatch"
        primary_upc = str((primary.get("external_ids") or {}).get("upc") or "")
        supplemental_upc = str((supplemental.get("external_ids") or {}).get("upc") or "")
        if primary_upc and supplemental_upc:
            return (primary_upc == supplemental_upc, "upc_mismatch" if primary_upc != supplemental_upc else "upc_match")
        if _identity_key(primary.get("album")) != _identity_key(supplemental.get("album")):
            return False, "album_mismatch"
        if not artist_identity_compatible(primary.get("album_artist"), supplemental.get("album_artist")):
            return False, "album_artist_mismatch"
        primary_tracks = self._tracks_by_position(primary)
        supplemental_tracks = self._tracks_by_position(supplemental)
        if set(primary_tracks) != set(supplemental_tracks):
            return False, "track_positions_mismatch"
        for position, primary_track in primary_tracks.items():
            supplemental_track = supplemental_tracks[position]
            if _identity_key(primary_track.get("title")) != _identity_key(supplemental_track.get("title")):
                return False, f"track_title_mismatch:{position[0]}:{position[1]}"
            if not artist_identity_compatible(primary_track.get("artist"), supplemental_track.get("artist")):
                return False, f"track_artist_mismatch:{position[0]}:{position[1]}"
        return True, "exact_metadata_match"

    def _apply_supplemental(
        self,
        primary: dict[str, Any],
        supplemental: dict[str, Any],
        fields: list[str],
    ) -> dict[str, Any]:
        merged = json.loads(json.dumps(primary))
        supplemental_tracks = self._tracks_by_position(supplemental)
        if "album" in fields:
            merged["album"] = supplemental.get("album")
        if "album_artist" in fields:
            merged["album_artist"] = supplemental.get("album_artist")
        if "date" in fields:
            merged["date"] = supplemental.get("date")
        for track in list(merged.get("tracks") or []):
            position = (_positive_int(track.get("disc_number"), 1), _positive_int(track.get("track_number"), 0))
            source_track = supplemental_tracks[position]
            if "title" in fields:
                track["title"] = source_track.get("title")
            if "artist" in fields:
                track["artist"] = source_track.get("artist")
            if "album" in fields:
                track["album"] = supplemental.get("album")
            if "album_artist" in fields:
                track["album_artist"] = supplemental.get("album_artist")
            if "date" in fields:
                track["date"] = supplemental.get("date")
        return merged

    @staticmethod
    def _simplified_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(json.dumps(manifest))
        for field in ("album", "album_artist"):
            result[field] = simplify_chinese(result.get(field))
        for track in list(result.get("tracks") or []):
            for field in ("title", "artist", "album", "album_artist"):
                track[field] = simplify_chinese(track.get(field))
        return result
