from __future__ import annotations

import hashlib
from pathlib import Path
import time
from typing import Any


TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def wait_for_operation(client, operation_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/operations/{operation_id}?full=true", headers=HEADERS)
        operation = response.json()["operation"]
        if operation["status"] in {"succeeded", "failed", "uncertain", "cancelled"}:
            return operation
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


def submit(client, action: str, input: dict[str, Any], request_id: str):
    return client.post("/operations", headers=HEADERS, json={"action": action, "input": input, "request_id": request_id})


def approve(client, approval_id: str, request_id: str):
    response = client.post(
        f"/approvals/{approval_id}/execute",
        headers=HEADERS,
        json={"request_id": request_id, "actor": "user", "reason": "reviewed exact preview", "approved_at": time.time()},
    )
    assert response.status_code in {200, 202}, response.text
    return wait_for_operation(client, response.json()["operation"]["operation_id"])


class FakeLibrary:
    def __init__(self):
        self.files: dict[str, dict[str, Any]] = {
            "Artist/Album/One.flac": self._row("One"),
            "Artist/Album/Two.flac": self._row("Two"),
        }

    @staticmethod
    def _row(title: str) -> dict[str, Any]:
        seed = f"file:{title}"
        return {
            "sha256": hashlib.sha256(seed.encode()).hexdigest(),
            "size": len(seed),
            "metadata": {"title": title, "artist": "Artist", "album": "Album"},
            "lyrics": "[00:01.00]original line",
            "cover_sha256": "",
        }

    def mutate_outside_ops(self, file_path: str) -> None:
        self.files[file_path]["sha256"] = hashlib.sha256(f"external:{time.time()}".encode()).hexdigest()

    def search_files(self, query: str, limit: int, max_scanned: int):
        rows = [
            {"file_path": path, "metadata": row["metadata"], "duration": "03:00", "warnings": []}
            for path, row in self.files.items()
            if query.casefold() in (path + " " + " ".join(row["metadata"].values())).casefold()
        ][:limit]
        return {"ok": True, "query": query, "limit": limit, "max_scanned": max_scanned, "result_count": len(rows), "results": rows}

    def resolve_path(self, file_path: str):
        exists = file_path in self.files
        return {
            "ok": True,
            "exists": exists,
            "is_file": exists,
            "resolved_path": f"/virtual-library/{file_path}",
        }

    def evidence_for_file(self, file_path: str, *, include=None, include_lyrics: bool = False):
        row = self.files.get(file_path)
        if row is None:
            return {"ok": True, "file_path": file_path, "exists": False, "metadata": {}, "lyrics": {}, "cover": {}}
        include = set(include or [])
        lyrics = {
            "present": bool(row["lyrics"]),
            "length": len(row["lyrics"]),
            "line_count": len(row["lyrics"].splitlines()),
            "sha256": hashlib.sha256(row["lyrics"].encode()).hexdigest(),
        }
        if include_lyrics or "lyrics_text" in include:
            lyrics["text"] = row["lyrics"]
        cover = {"present": bool(row["cover_sha256"]), "sha256": row["cover_sha256"]}
        if "cover_data" in include:
            cover["data_uri"] = "data:image/png;base64,iVBORw0KGgo="
        return {"ok": True, "file_path": file_path, "exists": True, "metadata": dict(row["metadata"]), "lyrics": lyrics, "cover": cover, "duration_seconds": 180, "warnings": []}

    def file_snapshot(self, file_path: str):
        row = self.files.get(file_path)
        if row is None:
            return {"ok": True, "file_path": file_path, "exists": False}
        evidence = self.evidence_for_file(file_path)
        return {"ok": True, "file_path": file_path, "exists": True, "sha256": row["sha256"], "size": row["size"], "metadata": evidence["metadata"], "lyrics": evidence["lyrics"], "cover": evidence["cover"]}

    def diagnose_album(self, **kwargs):
        return {"ok": True, "album": kwargs["album"], "local_track_count": len(self.files), "repair_plan": {"patches": []}}

    def metadata_patch(self, *, patches, apply=True):
        results = []
        for patch in patches:
            path = patch["file_path"]
            if path not in self.files:
                results.append({"ok": False, "file_path": path, "error_type": "missing_file"})
                continue
            self.files[path]["metadata"].update({key: str(value) for key, value in patch["fields"].items()})
            self.files[path]["sha256"] = hashlib.sha256((self.files[path]["sha256"] + "metadata").encode()).hexdigest()
            results.append({"ok": True, "verified": True, "file_path": path, "after": self.file_snapshot(path)})
        return {"ok": all(row["ok"] for row in results), "results": results}

    def lyrics_write(self, *, file_path, lyrics, lyrics_sha256, **_kwargs):
        if file_path not in self.files:
            return {"ok": False, "file_path": file_path, "error_type": "missing_file"}
        self.files[file_path]["lyrics"] = lyrics
        self.files[file_path]["sha256"] = hashlib.sha256((self.files[file_path]["sha256"] + lyrics_sha256).encode()).hexdigest()
        return {"ok": True, "verified": True, "file_path": file_path, "after": self.file_snapshot(file_path)}

    def cover_write(self, *, file_path, cover_sha256, **_kwargs):
        self.files[file_path]["cover_sha256"] = cover_sha256
        self.files[file_path]["sha256"] = hashlib.sha256((self.files[file_path]["sha256"] + cover_sha256).encode()).hexdigest()
        return {"ok": True, "verified": True, "file_path": file_path, "after": self.file_snapshot(file_path)}

    def move_file(self, *, file_path, target_file_path):
        if file_path not in self.files or target_file_path in self.files:
            return {"ok": False, "file_path": file_path, "error_type": "move_failed"}
        self.files[target_file_path] = self.files.pop(file_path)
        return {"ok": True, "verified": True, "file_path": file_path, "target_file_path": target_file_path}

    def delete_file(self, *, file_path):
        row = self.files.pop(file_path, None)
        return {"ok": row is not None, "verified": row is not None, "file_path": file_path, "sha256": (row or {}).get("sha256", "")}

    def add_file(self, *, source_path: Path, target_file_path: str, expected_sha256: str):
        if target_file_path in self.files:
            return {"ok": False, "target_file_path": target_file_path, "error_type": "target_exists"}
        data = source_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            return {"ok": False, "target_file_path": target_file_path, "error_type": "source_hash_mismatch"}
        self.files[target_file_path] = self._row(Path(target_file_path).stem)
        self.files[target_file_path]["sha256"] = expected_sha256
        return {"ok": True, "verified": True, "target_file_path": target_file_path, "sha256": expected_sha256, "size": len(data)}


class FakeNavidrome:
    def __init__(self):
        self.scans = 0

    def configured(self):
        return True

    def db_search_evidence(self, *, query, limit):
        return {"ok": True, "query": query, "limit": limit, "results": []}

    def path_evidence(self, **kwargs):
        return {"ok": True, "matched": True, **kwargs}

    def start_scan(self):
        self.scans += 1
        return {"ok": True, "started": True, "scan_number": self.scans}
