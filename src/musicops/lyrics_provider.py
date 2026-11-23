from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx


LRCLIB_API = "https://lrclib.net/api"
NETEASE_API = "https://music.163.com/api"
QQ_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
QQ_LYRIC_API = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LyricsProviderClient:
    """Server-side forwarding facade for configured public lyric providers."""

    def __init__(self, *, timeout: float = 12.0, client: httpx.Client | None = None):
        self.timeout = timeout
        self.client = client

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self.client is not None:
            response = self.client.request(method, url, **kwargs)
        else:
            response = httpx.request(method, url, timeout=self.timeout, follow_redirects=True, **kwargs)
        response.raise_for_status()
        return response

    def search(self, *, title: str, artist: str = "", album: str = "", limit: int = 10, quality: str = "balanced") -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        providers = ["lrclib"]
        if quality in {"balanced", "accurate"}:
            providers.append("netease")
        if quality == "accurate":
            providers.append("qq")
        for provider in providers:
            try:
                if provider == "lrclib":
                    candidates.extend(self._search_lrclib(title=title, artist=artist, album=album, limit=limit))
                elif provider == "netease":
                    candidates.extend(self._search_netease(title=title, artist=artist, limit=limit))
                else:
                    candidates.extend(self._search_qq(title=title, artist=artist, limit=limit))
            except Exception as exc:
                warnings.append({"code": "provider_search_failed", "detail": f"{provider}:{exc.__class__.__name__}"})
        return {
            "ok": bool(candidates),
            "query": {"title": title, "artist": artist, "album": album},
            "quality": quality,
            "providers": providers,
            "result_count": len(candidates[:limit]),
            "candidates": candidates[:limit],
            "warnings": warnings,
        }

    def fetch(self, *, provider: str, provider_id: str) -> dict[str, Any]:
        if provider == "lrclib":
            payload = self._request("GET", f"{LRCLIB_API}/get/{int(provider_id)}").json()
            canonical = str(payload.get("syncedLyrics") or payload.get("plainLyrics") or "").strip()
            identity = {
                "title": payload.get("trackName") or payload.get("name") or "",
                "artist": payload.get("artistName") or "",
                "album": payload.get("albumName") or "",
            }
            translated = ""
        elif provider == "netease":
            payload = self._request(
                "GET",
                f"{NETEASE_API}/song/lyric",
                params={"id": provider_id, "lv": -1, "tv": -1},
                headers={"User-Agent": "musicops/2"},
            ).json()
            canonical = str((payload.get("lrc") or {}).get("lyric") or "").strip()
            translated = str((payload.get("tlyric") or {}).get("lyric") or "").strip()
            identity = {}
        elif provider == "qq":
            payload = self._request(
                "GET",
                QQ_LYRIC_API,
                params={"songmid": provider_id, "format": "json", "g_tk": "5381"},
                headers={"Referer": "https://y.qq.com", "User-Agent": "musicops/2"},
            ).json()
            encoded = str(payload.get("lyric") or "")
            canonical = base64.b64decode(encoded).decode("utf-8").strip() if encoded else ""
            translated = ""
            identity = {}
        else:
            raise ValueError("unsupported_lyrics_provider")
        if not canonical:
            return {"ok": False, "provider": provider, "provider_id": provider_id, "detail": "lyrics_not_found"}
        return {
            "ok": True,
            "provider": provider,
            "provider_id": provider_id,
            "identity": identity,
            "canonical_lrc": canonical,
            "canonical_lrc_sha256": _sha256(canonical),
            "translated_lrc": translated,
            "translated_lrc_sha256": _sha256(translated) if translated else "",
        }

    def _search_lrclib(self, *, title: str, artist: str, album: str, limit: int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"{LRCLIB_API}/search",
            params={"track_name": title, "artist_name": artist, "album_name": album},
        ).json()
        return [
            {
                "provider": "lrclib",
                "provider_id": str(row.get("id") or ""),
                "title": row.get("trackName") or row.get("name") or "",
                "artist": row.get("artistName") or "",
                "album": row.get("albumName") or "",
                "duration_seconds": row.get("duration"),
                "synced": bool(row.get("syncedLyrics")),
            }
            for row in (payload if isinstance(payload, list) else [])[:limit]
        ]

    def _search_netease(self, *, title: str, artist: str, limit: int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"{NETEASE_API}/search/get/web",
            params={"s": " ".join(part for part in (title, artist) if part), "type": 1, "offset": 0, "limit": limit},
            headers={"User-Agent": "musicops/2"},
        ).json()
        return [
            {
                "provider": "netease",
                "provider_id": str(row.get("id") or ""),
                "title": row.get("name") or "",
                "artist": "; ".join(str(item.get("name") or "") for item in row.get("artists") or []),
                "album": (row.get("album") or {}).get("name") or "",
                "duration_seconds": round(float(row.get("duration") or 0) / 1000, 3),
            }
            for row in (payload.get("result") or {}).get("songs") or []
        ]

    def _search_qq(self, *, title: str, artist: str, limit: int) -> list[dict[str, Any]]:
        body = {
            "comm": {"ct": "6", "cv": "80600"},
            "music.search.SearchCgiService.DoSearchForQQMusicDesktop": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {"query": " ".join(part for part in (title, artist) if part), "num_per_page": limit, "page_num": 1},
            },
        }
        payload = self._request(
            "POST",
            QQ_API,
            json=body,
            headers={"Referer": "https://y.qq.com", "User-Agent": "musicops/2"},
        ).json()
        rows = (((payload.get("music.search.SearchCgiService.DoSearchForQQMusicDesktop") or {}).get("data") or {}).get("body") or {}).get("song") or {}
        return [
            {
                "provider": "qq",
                "provider_id": str(row.get("mid") or ""),
                "title": row.get("name") or "",
                "artist": "; ".join(str(item.get("name") or "") for item in row.get("singer") or []),
                "album": (row.get("album") or {}).get("name") or "",
                "duration_seconds": row.get("interval"),
            }
            for row in rows.get("list") or []
        ]
