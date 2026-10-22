"""Release text files must stay checkable by ``sha256sum -c``.

On Windows ``Path.write_text`` turns LF into CRLF; ``sha256sum`` then reads
every file name with a trailing carriage return and cannot open any asset.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from desktop.package_release import write_checksums, write_release_text


def test_checksums_use_lf_and_list_only_existing_assets(tmp_path: Path) -> None:
    (tmp_path / "TDL-Desktop-1.0.0.exe").write_bytes(b"exe")
    (tmp_path / "build-info.json").write_bytes(b"{}")

    target = write_checksums(tmp_path, ["TDL-Desktop-1.0.0.exe", "build-info.json", "missing.json"])

    data = target.read_bytes()
    assert b"\r" not in data
    exe = hashlib.sha256(b"exe").hexdigest()
    info = hashlib.sha256(b"{}").hexdigest()
    assert data == f"{exe}  TDL-Desktop-1.0.0.exe\n{info}  build-info.json\n".encode("ascii")


def test_build_info_uses_lf(tmp_path: Path) -> None:
    target = tmp_path / "build-info.json"
    report = {"application": "TDL 繁體中文下載助手", "version": "1.0.0"}

    write_release_text(target, json.dumps(report, ensure_ascii=False, indent=2))

    data = target.read_bytes()
    assert b"\r" not in data
    assert json.loads(data.decode("utf-8")) == report
