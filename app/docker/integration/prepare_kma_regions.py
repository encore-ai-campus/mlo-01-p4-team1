"""Import KMA mapping.json region-only rows to an EMPTY LOCAL PostgreSQL mapping table.

This does NOT import missing legal-ri or road-address data. No production DB access.
"""
import argparse
import json
import os
from importlib.resources import files
from pathlib import Path

from psycopg.types.json import Jsonb


def guard():
    if os.environ.get("MLO_LOCAL_INTEGRATION") != "1" or os.environ.get("MLO_LOCAL_DOCKER") != "1":
        raise RuntimeError("local_docker_explicit_flags_required")
    if (os.environ.get("PGHOST"), os.environ.get("PGDATABASE"), os.environ.get("PGUSER")) != (
        "postgres", "muther_dev", "muther_dev"
    ):
        raise RuntimeError("refusing_nonlocal_postgres")


def entries():
    from backend.weather.regions import bundled
    from pipeline.weather.prepare_regions import address
    data = json.loads(files("pipeline.weather").joinpath("mapping.json").read_text(encoding="utf-8"))
    result = list(bundled())
    known = {r["id"] for r in result}
    for r in data.get("admin_regions", []):
        identifier = "region:" + r["region_code"]
        if identifier in known:
            continue
        result.append(dict(
            id=identifier, name=r["name"].strip(), address=address(r),
            kind=r["kind"], level=r["level"],
            weather_region_code=r["region_code"], nx=r["nx"], ny=r["ny"],
            mapping_version=data["version"], mapping_basis="kma_admin_region",
        ))
        known.add(identifier)
    assert len(known) == len(result)
    return data["version"], result


def apply():
    from pipeline.weather.mapping_postgres import connection
    schema_sql = files("pipeline.weather").joinpath("mapping_postgres.sql").read_text(encoding="utf-8")
    version, regions = entries()
    with connection() as db:
        db.execute(schema_sql)
        db.execute("SELECT pg_advisory_xact_lock(3141625)")
        count = db.execute("SELECT count(*) AS n FROM weather_mapping.region").fetchone()["n"]
        if count:
            source = db.execute(
                "SELECT value FROM weather_mapping.metadata WHERE name='region_metadata'"
            ).fetchone()
            if not source:
                raise RuntimeError("unrecognized_existing_mapping_no_changes")
            meta = json.loads(source["value"])
            if meta.get("mapping_version") != version or meta.get("source_scope") != "kma_only_local_test":
                raise RuntimeError("existing_mapping_not_our_test_fixture")
            if count != len(regions):
                raise RuntimeError("partial_mapping_requires_manual_review")
            print(json.dumps({"result": "already_prepared", "region_rows": count}))
            return
        with db.cursor().copy(
            "COPY weather_mapping.region (id,weather_region_code,name,address,nx,ny,payload) FROM STDIN"
        ) as target:
            for row in regions:
                target.write_row((
                    row["id"], row.get("weather_region_code"), row["name"],
                    row["address"], row["nx"], row["ny"], Jsonb(row),
                ))
        metadata = dict(
            mapping_version=version, source_scope="kma_only_local_test",
            region_count=len(regions), admin_count=sum(r.get("level", 4) < 4 for r in regions),
            ri_count=0, road_count=0, entries=regions,
        )
        db.execute(
            "INSERT INTO weather_mapping.metadata(name,value) VALUES (%s,%s)",
            ("region_metadata", json.dumps(metadata, ensure_ascii=False)),
        )
    print(json.dumps(dict(result="prepared", region_rows=len(regions), legal_ri=False, roads=False)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write ONLY to local Docker DB")
    args = parser.parse_args()
    guard()
    version, rows = entries()
    if not args.apply:
        print(json.dumps(dict(dry_run=True, mapping_version=version, region_rows=len(rows))))
        return
    apply()


if __name__ == "__main__":
    main()
