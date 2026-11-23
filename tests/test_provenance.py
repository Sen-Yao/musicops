from __future__ import annotations

from musicops.provenance import verify_source_manifest, write_source_manifest


def test_source_manifest_is_deterministic_and_detects_stale_files(tmp_path):
    (tmp_path / "src" / "musicops").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='musicops'\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("MusicOps\n", encoding="utf-8")
    source = tmp_path / "src" / "musicops" / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    manifest = tmp_path / ".musicops-source-manifest"

    written = write_source_manifest(tmp_path, manifest)
    verified = verify_source_manifest(tmp_path, manifest, expected_sha256=written["manifest_sha256"])

    assert verified["verified"] is True
    source.write_text("VALUE = 2\n", encoding="utf-8")
    stale = verify_source_manifest(tmp_path, manifest, expected_sha256=written["manifest_sha256"])
    assert stale["verified"] is False
    assert stale["embedded_sha256"] == written["manifest_sha256"]


def test_source_manifest_rejects_appledouble_artifacts(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("project", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "src" / "._module.py").write_bytes(b"appledouble")

    try:
        write_source_manifest(tmp_path, tmp_path / "manifest")
    except ValueError as exc:
        assert str(exc) == "forbidden_source_artifact:src/._module.py"
    else:
        raise AssertionError("AppleDouble artifact must be rejected")
