"""Index the team CSV files for the venue MCP without changing their contents."""

import argparse
from contextlib import closing
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from tempfile import mkstemp
import zlib


def source_files(directories):
    paths = []
    for directory in directories:
        directory = directory.resolve()
        venues = sorted(directory.glob("*_venues_full.csv"))
        if not venues:
            raise ValueError(f"매장 원본이 없습니다: {directory}")
        for venue in venues:
            prefix = venue.name.removesuffix("_venues_full.csv")
            menus = directory / f"{prefix}_venue_menus_full.csv"
            if not menus.is_file():
                raise ValueError(f"메뉴 원본이 없습니다: {prefix}")
            paths.extend(((venue, "venues"), (menus, "menus")))
    if len({path.resolve() for path, _ in paths}) != len(paths):
        raise ValueError("같은 원본이 두 번 지정되었습니다.")
    return paths


def build(directories, output, *, replace=False):
    paths = source_files(directories)
    output = output.resolve()
    if output.exists() and not replace:
        raise ValueError("기존 파일을 갱신하려면 --replace를 지정해 주세요.")
    if output in {path.resolve() for path, _ in paths}:
        raise ValueError("원본 파일을 출력 경로로 사용할 수 없습니다.")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = mkstemp(prefix=".venue-catalog-", suffix=".sqlite3", dir=output.parent)
    os.close(descriptor)
    temporary = Path(name)
    counts, fingerprints = {}, []
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE venues (venue_id TEXT NOT NULL, raw TEXT NOT NULL);
                CREATE TABLE menus (menu_id TEXT NOT NULL, venue_id TEXT NOT NULL, raw TEXT NOT NULL);
            """)
            for path, kind in paths:
                before = path.stat()
                with path.open("rb") as source:
                    fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
                fingerprints.append(fingerprint)
                with path.open("r", encoding="utf-8-sig", newline="") as source:
                    reader = csv.DictReader(source)
                    required = {"venue_id", "매장명"} | ({"menu_id", "메뉴명"} if kind == "menus" else {"도로명주소", "업종구분"})
                    if not reader.fieldnames or not required.issubset(reader.fieldnames) or len(set(reader.fieldnames)) != len(reader.fieldnames):
                        raise ValueError(f"CSV 헤더를 확인해 주세요: {path.name}")
                    batch, count = [], 0
                    for row in reader:
                        if None in row or any(value is None for value in row.values()):
                            raise ValueError(f"CSV 열 수를 확인해 주세요: {path.name}")
                        raw = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                        record = (row["venue_id"], raw) if kind == "venues" else (row["menu_id"], row["venue_id"], raw)
                        batch.append(record)
                        count += 1
                        if len(batch) >= 2000:
                            db.executemany(f"INSERT INTO {kind} VALUES ({'?,?' if kind == 'venues' else '?,?,?'})", batch)
                            batch.clear()
                    if batch:
                        db.executemany(f"INSERT INTO {kind} VALUES ({'?,?' if kind == 'venues' else '?,?,?'})", batch)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError(f"변환 중 원본이 변경되었습니다: {path.name}")
                counts[path.name] = count
            db.execute("CREATE INDEX venues_id ON venues (venue_id)")
            db.execute("CREATE INDEX menus_venue_id ON menus (venue_id)")
            compressor = zlib.compressobj(level=3, wbits=16 + zlib.MAX_WBITS)
            parts = []
            for (raw,) in db.execute("SELECT raw FROM venues"):
                parts.append(compressor.compress(raw.encode("utf-8") + b"\n"))
            parts.append(compressor.flush())
            db.execute("INSERT INTO metadata VALUES (?, ?)", ("venue_rows_gzip", sqlite3.Binary(b"".join(parts))))
            version = "drive-" + hashlib.sha256("".join(fingerprints).encode()).hexdigest()[:12]
            metadata = {
                "schema": 1, "version": version, "preview_only": False,
                "source_basis": "팀 Drive 원본 매장·메뉴 CSV 입력값",
                "budget_basis": "원본에 기재된 1인 평균예산과 메뉴 가격",
                "tag_basis": "원본 분위기·메뉴명을 공통 태그로 연결",
                "source_counts": counts,
            }
            db.execute("INSERT INTO metadata VALUES (?, ?)", ("catalog", json.dumps(metadata, ensure_ascii=False)))
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("카탈로그 파일의 무결성 검사가 실패했습니다.")
        temporary.replace(output)
        return metadata
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        result = build(args.source_dir, args.output, replace=args.replace)
    except (OSError, ValueError, sqlite3.Error, csv.Error) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
