from __future__ import annotations

import base64
import hashlib

import pytest

from musicops.metadata_provider import MetadataProviderClient


class Response:
    def __init__(self, payload=None, *, content=b"", content_type="application/json"):
        self.payload = payload
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _release_payload(*, date="2026-07-20"):
    return {
        "id": "release-1",
        "title": "The Release",
        "date": date,
        "artist-credit": [{"name": "Future", "artist": {"name": "Future"}}],
        "release-group": {"id": "group-1"},
        "media": [
            {
                "position": 1,
                "track-count": 2,
                "tracks": [
                    {
                        "id": "track-1",
                        "position": 1,
                        "title": "One",
                        "recording": {"id": "recording-1", "title": "One"},
                    },
                    {
                        "id": "track-2",
                        "position": 2,
                        "title": "Two",
                        "recording": {"id": "recording-2", "title": "Two"},
                    },
                ],
            }
        ],
    }


def test_musicbrainz_search_returns_release_entities_without_field_merging():
    http = Http([
        Response({
            "releases": [
                {
                    "id": "standard",
                    "title": "Album",
                    "artist-credit": [{"name": "Artist", "artist": {"name": "Artist"}}],
                    "date": "2020-01-01",
                    "track-count": 10,
                    "release-group": {"id": "group-standard", "primary-type": "Album", "secondary-types": []},
                },
                {
                    "id": "deluxe",
                    "title": "Album (Deluxe)",
                    "artist-credit": [{"name": "Artist", "artist": {"name": "Artist"}}],
                    "date": "2021-01-01",
                    "track-count": 16,
                    "release-group": {"id": "group-deluxe", "primary-type": "Album", "secondary-types": []},
                },
            ]
        })
    ])
    client = MetadataProviderClient(client=http)

    result = client.search(query="Album", artist="Artist", album="Album", limit=10)

    assert result["ok"] is True
    assert [row["provider_release_id"] for row in result["candidates"]] == ["standard", "deluxe"]
    assert result["candidates"][0]["track_count"] == 10
    assert result["candidates"][1]["track_count"] == 16
    assert http.calls[0][2]["params"]["query"] == 'release:"Album" AND artist:"Artist"'


def test_musicbrainz_search_uses_recording_query_when_album_is_unknown():
    http = Http([Response({"releases": []})])
    client = MetadataProviderClient(client=http)

    result = client.search(query="Song", artist="Artist", album="", limit=10)

    assert result["ok"] is True
    assert http.calls[0][2]["params"]["query"] == 'recording:"Song" AND artist:"Artist"'


def test_musicbrainz_fetch_binds_complete_manifest_and_verified_cover():
    cover = b"\x89PNG\r\n\x1a\ncover"
    http = Http([
        Response(_release_payload()),
        Response(content=cover, content_type="image/png"),
    ])
    client = MetadataProviderClient(client=http)

    result = client.fetch(provider="musicbrainz", provider_release_id="release-1")

    assert result["ok"] is True
    manifest = result["manifest"]
    assert manifest["provider_release_id"] == "release-1"
    assert manifest["track_count"] == 2
    assert manifest["tracks"][0] == {
        "provider_track_id": "track-1",
        "provider_recording_id": "recording-1",
        "title": "One",
        "artist": "Future",
        "album": "The Release",
        "album_artist": "Future",
        "date": "2026-07-20",
        "track_number": 1,
        "track_total": 2,
        "disc_number": 1,
        "disc_total": 1,
    }
    assert result["manifest_sha256"]
    assert result["cover"]["sha256"] == hashlib.sha256(cover).hexdigest()
    assert base64.b64decode(result["cover"]["cover_base64"]) == cover
    assert "cover_base64" not in manifest["cover"]
    assert "fetched_at" not in manifest["source"]


def test_manifest_hash_is_stable_across_fetch_times(monkeypatch):
    cover = b"\x89PNG\r\n\x1a\ncover"
    first_http = Http([Response(_release_payload()), Response(content=cover, content_type="image/png")])
    second_http = Http([Response(_release_payload()), Response(content=cover, content_type="image/png")])
    times = iter([1000.0, 2000.0])
    monkeypatch.setattr("musicops.metadata_provider.time.time", lambda: next(times))

    first = MetadataProviderClient(client=first_http).fetch(provider="musicbrainz", provider_release_id="release-1")
    second = MetadataProviderClient(client=second_http).fetch(provider="musicbrainz", provider_release_id="release-1")

    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["manifest"] == second["manifest"]
    assert first["fetched_at"] != second["fetched_at"]


def test_musicbrainz_fetch_warns_when_optional_manifest_fields_are_missing():
    http = Http([Response(_release_payload(date=""))])
    client = MetadataProviderClient(client=http)

    result = client.fetch(provider="musicbrainz", provider_release_id="release-1")

    assert result["ok"] is True
    warning = next(w for w in result["warnings"] if w["code"] == "optional_metadata_missing")
    assert "date" in warning["fields"]
    assert "tracks[0].date" in warning["fields"]
    assert result["manifest"]["date"] == ""


def _provider_manifest(provider: str, *, date: str, title: str = "淚海", artist: str = "許茹芸"):
    track = {
        "provider_track_id": f"{provider}-track-1",
        "provider_recording_id": f"{provider}-recording-1",
        "title": title,
        "artist": artist,
        "album": title,
        "album_artist": artist,
        "date": date,
        "track_number": 1,
        "track_total": 1,
        "disc_number": 1,
        "disc_total": 1,
    }
    return {
        "provider": provider,
        "provider_release_id": f"{provider}-release-1",
        "release_group_id": "",
        "album": title,
        "album_artist": artist,
        "date": date,
        "disc_total": 1,
        "track_count": 1,
        "tracks": [track],
        "cover": {"status": "unavailable", "sha256": "", "size": 0, "media_type": ""},
    }


def test_search_aggregates_available_providers_and_warns_without_failing():
    class AggregateClient(MetadataProviderClient):
        def _search_musicbrainz(self, **_kwargs):
            return [{"provider": "musicbrainz", "provider_release_id": "mb-1"}]

        def _search_netease(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    client = AggregateClient(
        providers=["musicbrainz", "netease", "apple_music"],
        apple_music_token="",
    )

    result = client.search(query="泪海", artist="许茹芸", album="泪海")

    assert result["ok"] is True
    assert result["providers"] == ["musicbrainz"]
    assert result["candidates"] == [{"provider": "musicbrainz", "provider_release_id": "mb-1"}]
    assert {row["code"] for row in result["warnings"]} == {"provider_not_configured", "provider_search_failed"}


def test_netease_fetch_builds_a_complete_album_manifest():
    cover = b"\xff\xd8\xffcover"
    http = Http([
        Response({
            "code": 200,
            "album": {
                "id": 30544,
                "name": "泪海",
                "publishTime": 827942400000,
                "artists": [{"name": "许茹芸"}],
                "picUrl": "https://img.test/netease.jpg",
            },
            "songs": [
                {"id": 1, "name": "泪海", "ar": [{"name": "许茹芸"}], "no": 1, "cd": "01"},
                {"id": 2, "name": "秘密", "ar": [{"name": "许茹芸"}], "no": 2, "cd": "01"},
            ],
        }),
        Response(content=cover, content_type="image/jpeg"),
    ])
    client = MetadataProviderClient(providers=["netease"], client=http)

    result = client.fetch(provider="netease", provider_release_id="30544", market="cn")

    assert result["ok"] is True
    assert result["manifest"]["album"] == "泪海"
    assert result["manifest"]["tracks"][1]["track_number"] == 2
    assert result["manifest"]["track_count"] == 2


def test_provider_search_terms_use_simplified_chinese_for_simplifiable_services():
    http = Http([Response({"result": {"albums": []}})])
    client = MetadataProviderClient(providers=["netease"], client=http)

    result = client.search(query="淚海", artist="許茹芸", album="淚海", limit=3)

    assert result["ok"] is True
    assert http.calls[0][2]["params"]["s"] == "泪海 许茹芸"


def test_qq_fetch_builds_a_complete_album_manifest():
    cover = b"\xff\xd8\xffcover"
    http = Http([
        Response({
            "data": {
                "name": "泪海",
                "singername": "许茹芸",
                "aDate": "1996-03-28",
                "list": [
                    {"songmid": "qq-track-1", "songname": "泪海", "singer": [{"name": "许茹芸"}], "index_album": 1, "index_cd": 1},
                    {"songmid": "qq-track-2", "songname": "秘密", "singer": [{"name": "许茹芸"}], "index_album": 2, "index_cd": 1},
                ],
            }
        }),
        Response(content=cover, content_type="image/jpeg"),
    ])
    client = MetadataProviderClient(providers=["qq"], client=http)

    result = client.fetch(provider="qq", provider_release_id="qq-album", market="cn")

    assert result["ok"] is True
    assert result["manifest"]["album_artist"] == "许茹芸"
    assert result["manifest"]["tracks"][0]["provider_track_id"] == "qq-track-1"


def test_apple_music_fetch_uses_storefront_and_preserves_track_structure():
    cover = b"\x89PNG\r\n\x1a\ncover"
    http = Http([
        Response({
            "data": [{
                "id": "apple-album",
                "attributes": {
                    "name": "淚海",
                    "artistName": "許茹芸",
                    "releaseDate": "1996-03-28",
                    "upc": "123456789012",
                    "artwork": {"url": "https://img.test/{w}x{h}.png"},
                },
                "relationships": {
                    "tracks": {
                        "data": [{
                            "id": "apple-track-1",
                            "attributes": {
                                "name": "淚海",
                                "artistName": "許茹芸",
                                "trackNumber": 1,
                                "discNumber": 1,
                                "isrc": "TWAAA9600001",
                            },
                        }]
                    }
                },
            }]
        }),
        Response(content=cover, content_type="image/png"),
    ])
    client = MetadataProviderClient(providers=["apple_music"], apple_music_token="token", client=http)

    result = client.fetch(provider="apple_music", provider_release_id="apple-album", market="tw")

    assert result["ok"] is True
    assert result["manifest"]["album"] == "泪海"
    assert result["manifest"]["album_artist"] == "许茹芸"
    assert result["manifest"]["external_ids"]["upc"] == "123456789012"
    assert "/catalog/tw/albums/apple-album" in http.calls[0][1]


def test_spotify_fetch_uses_client_credentials_and_market():
    cover = b"\xff\xd8\xffcover"
    http = Http([
        Response({"access_token": "spotify-token", "expires_in": 3600}),
        Response({
            "id": "spotify-album",
            "name": "The Album",
            "artists": [{"name": "The Artist"}],
            "release_date": "2020-01-02",
            "external_ids": {"upc": "987654321098"},
            "images": [{"url": "https://img.test/spotify.jpg"}],
            "tracks": {
                "items": [{
                    "id": "spotify-track-1",
                    "name": "The Song",
                    "artists": [{"name": "The Artist"}],
                    "track_number": 1,
                    "disc_number": 1,
                }],
                "next": None,
            },
        }),
        Response(content=cover, content_type="image/jpeg"),
    ])
    client = MetadataProviderClient(
        providers=["spotify"],
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        client=http,
    )

    result = client.fetch(provider="spotify", provider_release_id="spotify-album", market="US")

    assert result["ok"] is True
    assert result["manifest"]["album"] == "The Album"
    assert result["manifest"]["external_ids"]["upc"] == "987654321098"
    assert http.calls[0][1].endswith("/token")
    assert http.calls[1][2]["params"]["market"] == "US"


def test_fetch_can_mix_bound_sources_and_simplifies_final_chinese_metadata():
    class MixedClient(MetadataProviderClient):
        def _fetch_one(self, provider, provider_release_id, *, market=""):
            date = "1996-03-28" if provider == "netease" else "1996"
            manifest = _provider_manifest(provider, date=date)
            manifest["provider_release_id"] = provider_release_id
            return {
                "ok": True,
                "provider": provider,
                "manifest": manifest,
                "cover": {"status": "unavailable", "sha256": "", "size": 0, "media_type": ""},
            }

    client = MixedClient(providers=["musicbrainz", "netease"])

    result = client.fetch(
        provider="musicbrainz",
        provider_release_id="mb-release",
        supplemental_sources=[{
            "provider": "netease",
            "provider_release_id": "netease-release",
            "fields": ["date"],
        }],
    )

    assert result["ok"] is True
    assert result["provider"] == "mixed"
    assert result["manifest"]["album"] == "泪海"
    assert result["manifest"]["album_artist"] == "许茹芸"
    assert result["manifest"]["date"] == "1996-03-28"
    assert result["manifest"]["tracks"][0]["date"] == "1996-03-28"
    assert result["manifest"]["source"]["providers"] == ["musicbrainz", "netease"]


def test_fetch_skips_incompatible_supplemental_without_merging():
    class MixedClient(MetadataProviderClient):
        def _fetch_one(self, provider, provider_release_id, *, market=""):
            title = "另一张专辑" if provider == "netease" else "淚海"
            manifest = _provider_manifest(provider, date="1996-03-28", title=title)
            manifest["provider_release_id"] = provider_release_id
            return {
                "ok": True,
                "provider": provider,
                "manifest": manifest,
                "cover": {"status": "unavailable", "sha256": "", "size": 0, "media_type": ""},
            }

    client = MixedClient(providers=["musicbrainz", "netease"])

    result = client.fetch(
        provider="musicbrainz",
        provider_release_id="mb-release",
        supplemental_sources=[{
            "provider": "netease",
            "provider_release_id": "netease-release",
            "fields": ["date"],
        }],
    )

    assert result["ok"] is True
    assert result["provider"] == "musicbrainz"
    assert result["manifest"]["album"] == "泪海"
    assert result["warnings"][0]["code"] == "supplemental_source_skipped"
    assert len(result["sources"]) == 1


def test_fetch_accepts_collaborator_separator_variants_across_bound_sources():
    class MixedClient(MetadataProviderClient):
        def _fetch_one(self, provider, provider_release_id, *, market=""):
            artist = "Orton; 班克西 8ank$Yy" if provider == "musicbrainz" else "Orton&班克西 8ank$Yy"
            manifest = _provider_manifest(provider, date="2026", title="turn it up", artist=artist)
            manifest["provider_release_id"] = provider_release_id
            return {
                "ok": True,
                "provider": provider,
                "manifest": manifest,
                "cover": {"status": "unavailable", "sha256": "", "size": 0, "media_type": ""},
            }

    client = MixedClient(providers=["musicbrainz", "netease"])
    result = client.fetch(
        provider="musicbrainz",
        provider_release_id="mb-release",
        supplemental_sources=[{"provider": "netease", "provider_release_id": "netease-release", "fields": ["date"]}],
    )
    assert result["ok"] is True


def test_missing_essential_title_still_rejected():
    payload = _release_payload()
    for medium in payload["media"]:
        for track in medium["tracks"]:
            track["title"] = ""
            track["recording"]["title"] = ""
    result = MetadataProviderClient(client=Http([Response(payload)])).fetch(provider="musicbrainz",provider_release_id="release-1")
    assert result["ok"] is False
    assert result["error"] == "canonical_manifest_incomplete"
    assert "tracks[0].title" in result["missing_fields"]


@pytest.mark.parametrize("failure", ["exception", "error", "unconfigured"])
def test_optional_supplement_failure_retains_primary(failure):
    class Client(MetadataProviderClient):
        def _fetch_one(self, provider, provider_release_id, *, market=""):
            if provider == "netease":
                if failure == "exception":
                    raise RuntimeError("provider unavailable")
                return {"ok":False,"error":"provider_unavailable"}
            manifest = _provider_manifest(provider,date="1996-03-28")
            manifest["provider_release_id"] = provider_release_id
            return {"ok":True,"manifest":manifest,"cover":{"status":"unavailable"}}
    client = Client(providers=["musicbrainz"] if failure == "unconfigured" else ["musicbrainz","netease"])
    result=client.fetch(provider="musicbrainz",provider_release_id="release-1",supplemental_sources=[{"provider":"netease","provider_release_id":"other","fields":["date"]}])
    assert result["ok"] is True
    assert result["manifest"]["date"] == "1996-03-28"
    assert result["warnings"][0]["code"] == "supplemental_source_skipped"
    assert len(result["sources"]) == 1
