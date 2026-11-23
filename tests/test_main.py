import importlib

from fastapi.testclient import TestClient

from musicops.actions import build_registry, registry_digest
from musicops.api import create_app
from musicops.provenance import write_source_manifest


def test_main_app_uses_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSICOPS_API_TOKEN", "env-token")
    monkeypatch.setenv("MUSICOPS_DATA_DIR", str(tmp_path))
    import musicops.main

    module = importlib.reload(musicops.main)
    with TestClient(module.app) as client:
        body = client.get("/actions", headers={"Authorization": "Bearer env-token"}).json()
        assert body["ok"] is True
        assert body["actions"]


def test_main_requires_token(monkeypatch):
    monkeypatch.delenv("MUSICOPS_API_TOKEN", raising=False)
    monkeypatch.delenv("MUSICOPS_API_TOKEN_FILE", raising=False)
    import musicops.main

    try:
        importlib.reload(musicops.main)
    except RuntimeError as exc:
        assert str(exc) == "MUSICOPS_API_TOKEN or MUSICOPS_API_TOKEN_FILE is required"
    else:
        raise AssertionError("main import must fail without a token")


def test_main_reads_token_file_when_environment_token_is_absent(monkeypatch, tmp_path):
    token_file = tmp_path / "musicops-api-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.delenv("MUSICOPS_API_TOKEN", raising=False)
    monkeypatch.setenv("MUSICOPS_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("MUSICOPS_DATA_DIR", str(tmp_path / "data"))
    import musicops.main

    module = importlib.reload(musicops.main)
    with TestClient(module.app) as client:
        assert client.get("/actions", headers={"Authorization": "Bearer file-token"}).status_code == 200
        assert client.get("/actions", headers={"Authorization": "Bearer env-token"}).status_code == 401


def test_readiness_reports_and_enforces_release_provenance(monkeypatch, tmp_path):
    source_root = tmp_path / "app"
    (source_root / "src" / "musicops").mkdir(parents=True)
    (source_root / "pyproject.toml").write_text("[project]\nname='musicops'\n", encoding="utf-8")
    (source_root / "README.md").write_text("MusicOps\n", encoding="utf-8")
    (source_root / "src" / "musicops" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest_path = source_root / ".musicops-source-manifest"
    source_digest = write_source_manifest(source_root, manifest_path)["manifest_sha256"]
    schema_digest = registry_digest(build_registry())
    expected = {
        "MUSICOPS_BUILD_COMMIT": "a" * 40,
        "MUSICOPS_BUILD_TREE": "b" * 40,
        "MUSICOPS_BUILD_TIME": "2026-08-03T00:00:00Z",
        "MUSICOPS_SOURCE_MANIFEST_SHA256": source_digest,
        "MUSICOPS_ACTION_SCHEMA_DIGEST": schema_digest,
        "MUSICOPS_IMAGE_ID": "sha256:" + "c" * 64,
        "MUSICOPS_SOURCE_ROOT": str(source_root),
        "MUSICOPS_SOURCE_MANIFEST_PATH": str(manifest_path),
    }
    for key, value in expected.items():
        monkeypatch.setenv(key, value)

    app = create_app(tmp_path / "data", "token")
    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        build = response.json()["build"]
        assert build["git_commit"] == expected["MUSICOPS_BUILD_COMMIT"]
        assert build["git_tree"] == expected["MUSICOPS_BUILD_TREE"]
        assert build["source_manifest_sha256"] == source_digest
        assert build["action_schema_digest"] == schema_digest
        assert build["image_id"] == expected["MUSICOPS_IMAGE_ID"]
        assert build["source_provenance"]["verified"] is True

    monkeypatch.setenv("MUSICOPS_ACTION_SCHEMA_DIGEST", "0" * 64)
    mismatched = create_app(tmp_path / "mismatched", "token")
    with TestClient(mismatched) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["action_schema_provenance"] is False
