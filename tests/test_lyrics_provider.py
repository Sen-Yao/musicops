from __future__ import annotations

from musicops.lyrics_provider import LyricsProviderClient, NETEASE_API


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Http:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response(self.payload)


def test_netease_fetch_requests_full_current_lyrics_without_cached_versions():
    canonical = "[00:01.00]Original line"
    translated = "[00:01.00]Translated line"
    http = Http({
        "code": 200,
        "lrc": {"version": 1, "lyric": canonical},
        "tlyric": {"version": 1, "lyric": translated},
    })

    result = LyricsProviderClient(client=http).fetch(provider="netease", provider_id="123")

    assert result["ok"] is True
    assert result["canonical_lrc"] == canonical
    assert result["translated_lrc"] == translated
    assert http.calls == [(
        "GET",
        f"{NETEASE_API}/song/lyric",
        {
            "params": {"id": "123", "lv": -1, "tv": -1},
            "headers": {"User-Agent": "musicops/2"},
        },
    )]
