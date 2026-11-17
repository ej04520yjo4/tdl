"""Release text files must stay checkable by ``sha256sum -c``.

On Windows ``Path.write_text`` turns LF into CRLF; ``sha256sum`` then reads
every file name with a trailing carriage return and cannot open any asset.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from desktop.package_release import core_modules, module_cache_name, write_checksums, write_release_text


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


def test_only_modules_linked_into_the_core_are_listed() -> None:
    build_info = "\n".join(
        [
            "bin/tdl-core.exe: go1.27.3",
            "\tpath\tgithub.com/iyear/tdl",
            "\tmod\tgithub.com/iyear/tdl\tv0.0.0-1\t",
            "\tdep\tgithub.com/AlecAivazis/survey/v2\tv2.3.7\th1:abc=",
            "\tdep\tgithub.com/iyear/tdl/core\t(devel)\t",
            "\tdep\texample.com/old\tv1.0.0\th1:old=",
            "\t=>\texample.com/new\tv1.1.0\th1:new=",
            "\tbuild\t-trimpath=true",
        ]
    )
    assert core_modules(build_info) == [
        ("github.com/AlecAivazis/survey/v2", "v2.3.7"),
        ("example.com/new", "v1.1.0"),
    ]


def test_module_cache_name_escapes_capitals() -> None:
    assert module_cache_name("github.com/AlecAivazis/survey/v2", "v2.3.7") == "github.com/!alec!aivazis/survey/v2@v2.3.7"
