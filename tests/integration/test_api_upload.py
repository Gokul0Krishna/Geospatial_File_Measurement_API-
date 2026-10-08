import io

import pytest
from pyproj import CRS, Transformer

from tests.conftest import FailingDispatcher, NoopDispatcher, upload
from tests.helpers import (
    BLR_HOLE,
    BLR_SQUARE,
    build_shapefile_zip,
    build_zip,
    geodesic_line_length,
    geodesic_ring_area,
    kml_document,
    kml_line,
    kml_placemark,
    kml_point,
    kml_polygon,
)

LINE = [(77.59, 12.97), (77.60, 12.99)]
EXPECTED_HOLED_AREA = geodesic_ring_area(BLR_SQUARE) - geodesic_ring_area(BLR_HOLE)


def test_kml_upload_end_to_end(client):
    mixed = f"<MultiGeometry>{kml_point(77.5, 12.9)}{kml_line(LINE)}</MultiGeometry>"
    kml = kml_document(
        (
            "Survey",
            [
                kml_placemark("parcel", kml_polygon(BLR_SQUARE, [BLR_HOLE]), {"owner": "Alice"}),
                kml_placemark("road", kml_line(LINE)),
                kml_placemark("well", kml_point(77.595, 12.975)),
                kml_placemark("mixed", mixed),
                kml_placemark("nogeom"),
            ],
        )
    )
    response = upload(client, kml, "survey.kml")
    assert response.status_code == 201
    body = response.json()
    assert response.headers["location"] == f"/api/files/{body['id']}/"
    assert body["filename"] == "survey.kml"
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 5
    assert body["crs"] == "EPSG:4326"
    assert body["source_format"] == "kml"

    info = client.get(f"/api/files/{body['id']}/").json()
    assert info["status"] == "COMPLETED" and info["feature_count"] == 5

    page = client.get(f"/api/files/{body['id']}/measurements/").json()
    items = {i["feature_index"]: i for i in page["items"]}
    assert [items[i]["status"] for i in range(5)] == [
        "MEASURED", "MEASURED", "NOT_APPLICABLE", "UNSUPPORTED", "EMPTY",
    ]

    parcel = items[0]["measurement"]
    assert parcel["type"] == "area" and parcel["unit"] == "m2" and parcel["method"] == "utm"
    assert parcel["crs"] == "EPSG:32643"
    assert parcel["value"] == pytest.approx(EXPECTED_HOLED_AREA, rel=3e-3)

    road = items[1]["measurement"]
    assert road["type"] == "length" and road["unit"] == "m"
    assert road["value"] == pytest.approx(geodesic_line_length(LINE), rel=3e-3)

    assert items[2]["measurement"] is None and "no area" in items[2]["reason"].lower()
    assert items[3]["geometry_type"] == "GeometryCollection"
    assert items[3]["measurement"] is None and "not supported" in items[3]["reason"]
    assert "geometry" not in items[0]  # only present with include_geometry=true

    summary = page["summary"]
    assert summary["by_status"] == {"MEASURED": 2, "NOT_APPLICABLE": 1, "UNSUPPORTED": 1, "EMPTY": 1}
    assert summary["total_area_m2"]["utm"] == pytest.approx(EXPECTED_HOLED_AREA, rel=3e-3)


def test_features_endpoint_returns_geometry_crs_and_properties(client):
    kml = kml_document(("F", [kml_placemark("parcel", kml_polygon(BLR_SQUARE), {"owner": "Alice"})]))
    file_id = upload(client, kml, "p.kml").json()["id"]
    item = client.get(f"/api/files/{file_id}/features/").json()["items"][0]
    assert item["crs"] == "EPSG:4326"
    assert item["properties"] == {"name": "parcel", "owner": "Alice"}
    assert item["geometry"]["type"] == "Polygon"
    assert item["geometry"]["coordinates"][0][0][:2] == [77.59, 12.97]
    assert item["measurement"]["value"] > 9e5


def test_geodesic_method_is_selectable(client):
    file_id = upload(client, kml_document(("F", [kml_placemark("p", kml_polygon(BLR_SQUARE))])), "p.kml").json()["id"]
    geo = client.get(f"/api/files/{file_id}/measurements/", params={"method": "geodesic"}).json()
    value = geo["items"][0]["measurement"]
    assert geo["method"] == "geodesic"
    assert value["crs"] == "ellipsoid:WGS84"
    assert value["value"] == pytest.approx(geodesic_ring_area(BLR_SQUARE), rel=1e-9)
    assert client.get(f"/api/files/{file_id}/measurements/", params={"method": "nope"}).status_code == 422


def test_shapefile_upload_with_null_shape_and_attributes(client):
    data = build_shapefile_zip(
        "polygon", [[BLR_SQUARE, BLR_HOLE], None], records=[("parcel", 3.5), ("ghost", None)]
    )
    response = upload(client, data, "parcels.zip")
    assert response.status_code == 201
    body = response.json()
    assert (body["status"], body["feature_count"], body["crs"]) == ("COMPLETED", 2, "EPSG:4326")

    items = client.get(f"/api/files/{body['id']}/features/").json()["items"]
    assert items[0]["properties"] == {"name": "parcel", "value": 3.5}
    assert items[0]["measurement"]["value"] == pytest.approx(EXPECTED_HOLED_AREA, rel=3e-3)
    assert items[1]["status"] == "EMPTY" and items[1]["geometry"] is None


def test_shapefile_lines_and_points(client):
    lines = upload(client, build_shapefile_zip("line", [LINE]), "roads.zip").json()
    item = client.get(f"/api/files/{lines['id']}/measurements/").json()["items"][0]
    assert item["measurement"]["type"] == "length"
    points = upload(client, build_shapefile_zip("point", [(77.6, 12.9)]), "pts.zip").json()
    item = client.get(f"/api/files/{points['id']}/measurements/").json()["items"][0]
    assert item["status"] == "NOT_APPLICABLE" and item["measurement"] is None


def test_projected_shapefile_is_measured_correctly_not_in_web_mercator(client):
    """The headline CRS test: a Web Mercator file at 60N must report TRUE area."""
    helsinki = [(24.90, 60.17), (24.91, 60.17), (24.91, 60.175), (24.90, 60.175)]
    to_3857 = Transformer.from_crs(4326, 3857, always_xy=True)
    projected = [to_3857.transform(x, y) for x, y in helsinki]
    data = build_shapefile_zip("polygon", [[projected]], crs=CRS.from_epsg(3857))
    body = upload(client, data, "helsinki.zip").json()
    assert body["crs"] == "EPSG:3857"
    item = client.get(f"/api/files/{body['id']}/measurements/").json()["items"][0]
    assert item["measurement"]["crs"] == "EPSG:32635"
    assert item["measurement"]["value"] == pytest.approx(geodesic_ring_area(helsinki), rel=3e-3)


def test_missing_prj_rejected_in_strict_mode(client):
    response = upload(client, build_shapefile_zip("polygon", [[BLR_SQUARE]], include_prj=False), "x.zip")
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert ".prj" in response.json()["detail"]


def test_missing_prj_assumes_crs_in_lenient_mode_and_says_so(make_client):
    client = make_client(REQUIRE_PRJ=False)
    response = upload(client, build_shapefile_zip("polygon", [[BLR_SQUARE]], include_prj=False), "x.zip")
    body = response.json()
    assert response.status_code == 201 and body["status"] == "COMPLETED"
    assert body["warnings"] == ["CRS_ASSUMED:EPSG:4326"]
    assert body["crs"] == "EPSG:4326"


def test_unreadable_prj_fails_processing_with_clear_message(client):
    good = build_shapefile_zip("polygon", [[BLR_SQUARE]])
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, b"this is not WKT" if info.filename.endswith(".prj") else src.read(info))
    body = upload(client, out.getvalue(), "badprj.zip").json()
    assert body["status"] == "FAILED"
    assert "coordinate reference system" in body["error"]


def test_corrupt_shapefile_ends_up_failed_not_500(client):
    data = build_zip({"a.shp": b"garbage" * 50, "a.shx": b"x" * 100, "a.dbf": b"y" * 100, "a.prj": b"GEOGCS[]"})
    response = upload(client, data, "bad.zip")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FAILED" and "could not be opened" in body["error"]
    not_ready = client.get(f"/api/files/{body['id']}/measurements/")
    assert not_ready.status_code == 409 and "Processing failed" in not_ready.json()["detail"]


@pytest.mark.parametrize(
    ("filename", "data", "status"),
    [
        ("notes.txt", b"hello", 415),
        ("data.geojson", b"{}", 415),
        ("empty.kml", b"", 400),
        ("fake.zip", b"definitely not a zip", 400),
        ("fake.kml", b"<html><body>hi</body></html>", 400),
    ],
)
def test_invalid_uploads_are_rejected_with_problem_json(client, filename, data, status):
    response = upload(client, data, filename)
    assert response.status_code == status
    body = response.json()
    assert body["status"] == status and body["type"].startswith("urn:geo-measure:problem:")
    assert body["request_id"]


def test_missing_file_part_is_a_422(client):
    response = client.post("/api/files/")
    assert response.status_code == 422 and response.json()["errors"]


def test_kml_with_entity_declaration_is_rejected(client):
    evil = b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaa">]><kml><Document/></kml>'
    response = upload(client, evil, "evil.kml")
    assert response.status_code == 400 and "entity" in response.json()["detail"]


def test_zip_slip_and_zip_bomb_rejected_at_upload(client):
    parts = {"a.shp": b"s", "a.shx": b"x", "a.dbf": b"d", "a.prj": b"p"}
    assert upload(client, build_zip({**parts, "../../evil.txt": b"x"}), "slip.zip").status_code == 400
    bomb = upload(client, build_zip({**parts, "filler.bin": b"\x00" * (8 * 1024 * 1024)}), "bomb.zip")
    assert bomb.status_code == 400 and "compression ratio" in bomb.json()["detail"]


def test_filename_is_sanitised_and_never_used_as_a_path(client):
    kml = kml_document(("F", [kml_placemark("p", kml_point(1, 1))]))
    body = upload(client, kml, "../../etc/passwd.kml").json()
    assert body["filename"] == "passwd.kml"
    assert list(client.app.state.storage.files_dir.iterdir())[0].name == body["id"]  # id, not name


def test_upload_size_cap_on_bytes_read(make_client):
    client = make_client(MAX_UPLOAD_BYTES=20_000, MAX_KML_BYTES=10_000)
    big_kml = kml_document(("F", [kml_placemark(f"p{i}", kml_point(1, 1)) for i in range(150)]))
    assert 10_000 < len(big_kml) < 20_000
    response = upload(client, big_kml, "big.kml")
    assert response.status_code == 413 and response.json()["type"].endswith("payload-too-large")


def test_declared_content_length_over_cap_is_rejected_before_reading(make_client):
    client = make_client(MAX_UPLOAD_BYTES=20_000, MAX_KML_BYTES=10_000)
    assert upload(client, b"<kml>" + b"x" * (2 * 1024 * 1024), "huge.kml").status_code == 413


def test_chunked_upload_without_content_length_is_cut_off(make_client):
    """The cap is enforced on bytes as they arrive, not on a header the client controls."""
    client = make_client(MAX_UPLOAD_BYTES=20_000, MAX_KML_BYTES=10_000)
    boundary = "xBOUNDARYx"

    def body():
        yield f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.kml"\r\n\r\n'.encode()
        for _ in range(40):
            yield b"<kml>" + b"x" * 60_000  # ~2.4 MB in 60 KB chunks
        yield f"\r\n--{boundary}--\r\n".encode()

    response = client.post(
        "/api/files/",
        content=body(),
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 413
    assert response.json()["type"].endswith("payload-too-large")


def test_per_client_backpressure(make_client):
    client = make_client(NoopDispatcher(), MAX_ACTIVE_FILES_PER_CLIENT=1)
    kml = kml_document(("F", [kml_placemark("p", kml_point(1, 1))]))
    assert upload(client, kml, "a.kml").status_code == 202  # queued, stays PENDING
    second = upload(client, kml, "b.kml")
    assert second.status_code == 429 and second.headers["retry-after"]
    assert second.json()["type"].endswith("too-many-active-files")


def test_global_backpressure(make_client):
    client = make_client(NoopDispatcher(), MAX_INFLIGHT_JOBS=1)
    kml = kml_document(("F", [kml_placemark("p", kml_point(1, 1))]))
    assert upload(client, kml, "a.kml").status_code == 202
    busy = upload(client, kml, "b.kml")
    assert busy.status_code == 503 and busy.headers["retry-after"]
    assert busy.json()["type"].endswith("service-busy")


def test_queue_outage_returns_503_and_leaves_nothing_behind(make_client):
    client = make_client(FailingDispatcher())
    kml = kml_document(("F", [kml_placemark("p", kml_point(1, 1))]))
    response = upload(client, kml, "a.kml")
    assert response.status_code == 503
    assert client.get("/api/files/").json()["items"] == []
    storage = client.app.state.storage
    assert list(storage.files_dir.iterdir()) == []


def test_worker_mode_shape_is_202_pending(make_client):
    client = make_client(NoopDispatcher())
    kml = kml_document(("F", [kml_placemark("p", kml_point(1, 1))]))
    response = upload(client, kml, "a.kml")
    assert response.status_code == 202
    assert response.json()["status"] == "PENDING" and response.json()["feature_count"] is None
