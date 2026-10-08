import pytest

from tests.conftest import NoopDispatcher, upload
from tests.helpers import BLR_SQUARE, build_shapefile_zip, build_zip, kml_document, kml_placemark, kml_point


def polygons(n: int) -> list:
    out = []
    for i in range(n):
        dx = i * 0.002
        out.append([[(77.59 + dx, 12.97), (77.591 + dx, 12.97), (77.591 + dx, 12.971), (77.59 + dx, 12.971)]])
    return out


def walk(client, url, params=None):
    """Follow next_cursor to the end; returns (pages, all items)."""
    pages, items, cursor = [], [], None
    while True:
        query = {**(params or {}), **({"cursor": cursor} if cursor else {})}
        response = client.get(url, params=query)
        assert response.status_code == 200, response.text
        page = response.json()
        pages.append(page)
        items += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            return pages, items


@pytest.fixture
def file25(client):
    data = build_shapefile_zip("polygon", polygons(25))
    return upload(client, data, "p25.zip").json()["id"]


def test_keyset_pagination_covers_every_feature_exactly_once(client, file25):
    pages, items = walk(client, f"/api/files/{file25}/measurements/", {"limit": 10})
    assert [p["count"] for p in pages] == [10, 10, 5]
    assert [i["feature_index"] for i in items] == list(range(25))
    assert pages[-1]["next_cursor"] is None


def test_invalid_limits_and_cursors(client, file25):
    url = f"/api/files/{file25}/measurements/"
    assert client.get(url, params={"cursor": "garbage!!"}).status_code == 400
    assert client.get(url, params={"limit": 0}).status_code == 422
    assert client.get(url, params={"limit": 100_000}).status_code == 422
    too_many = client.get(url, params={"limit": 500, "include_geometry": "true"})
    assert too_many.status_code == 422 and "include_geometry" in too_many.json()["detail"]
    assert client.get(url, params={"limit": 100, "include_geometry": "true"}).status_code == 200


def test_geometry_pages_are_capped_by_bytes_but_pagination_still_complete(make_client):
    client = make_client(PAGE_MAX_GEOMETRY_BYTES=300)  # ~one 93-byte polygon each: a handful
    file_id = upload(client, build_shapefile_zip("polygon", polygons(12)), "p.zip").json()["id"]
    pages, items = walk(client, f"/api/files/{file_id}/measurements/", {"limit": 10, "include_geometry": "true"})
    assert len(pages) > 2 and max(p["count"] for p in pages) < 10
    assert [i["feature_index"] for i in items] == list(range(12))
    assert all(i["geometry"]["type"] == "Polygon" for i in items)


def test_status_filter(client):
    data = build_shapefile_zip("polygon", [[BLR_SQUARE], None, [BLR_SQUARE], None])
    file_id = upload(client, data, "mix.zip").json()["id"]
    empty = client.get(f"/api/files/{file_id}/measurements/", params={"status": "EMPTY"}).json()
    assert [i["feature_index"] for i in empty["items"]] == [1, 3]
    assert client.get(f"/api/files/{file_id}/measurements/", params={"status": "BOGUS"}).status_code == 422


def test_unknown_file_is_404_problem_json(client):
    for url in ("/api/files/nope/", "/api/files/nope/measurements/", "/api/files/nope/features/"):
        response = client.get(url)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["type"].endswith("file-not-found")
    assert client.get("/no/such/route").status_code == 404


def test_measurements_before_completion_is_409_with_retry_after(make_client):
    client = make_client(NoopDispatcher())
    kml = kml_document(("F", [kml_placemark("p", kml_point(1, 1))]))
    file_id = upload(client, kml, "a.kml").json()["id"]
    assert client.get(f"/api/files/{file_id}/").json()["status"] == "PENDING"
    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 409
    body = response.json()
    assert body["status"] == 409  # RFC 7807 member is the HTTP status, not clobbered by extras
    assert body["file_status"] == "PENDING" and "PENDING" in body["detail"]
    assert response.headers["retry-after"]


def test_list_files_newest_first_with_cursor(client):
    kml = kml_document(("F", [kml_placemark("p", kml_point(1, 1))]))
    ids = [upload(client, kml, f"f{i}.kml").json()["id"] for i in range(3)]
    first = client.get("/api/files/", params={"limit": 2}).json()
    assert [f["id"] for f in first["items"]] == [ids[2], ids[1]]
    second = client.get("/api/files/", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [f["id"] for f in second["items"]] == [ids[0]] and second["next_cursor"] is None
    assert client.get("/api/files/", params={"status": "FAILED"}).json()["items"] == []


def test_retry_only_applies_to_failed_files(client):
    bad = build_zip({"a.shp": b"garbage" * 50, "a.shx": b"x" * 100, "a.dbf": b"y" * 100, "a.prj": b"GEOGCS[]"})
    failed_id = upload(client, bad, "bad.zip").json()["id"]
    retried = client.post(f"/api/files/{failed_id}/retry/")
    assert retried.status_code == 202 and retried.json()["status"] == "FAILED"  # still corrupt

    ok_id = upload(client, kml_document(("F", [kml_placemark("p", kml_point(1, 1))])), "ok.kml").json()["id"]
    conflict = client.post(f"/api/files/{ok_id}/retry/")
    assert conflict.status_code == 409 and conflict.json()["type"].endswith("invalid-state")
    assert client.post("/api/files/nope/retry/").status_code == 404


def test_delete_removes_file_features_and_blob(client, file25):
    storage = client.app.state.storage
    assert (storage.files_dir / file25).exists()
    response = client.delete(f"/api/files/{file25}/")
    assert response.status_code == 202 and response.json() == {"id": file25, "status": "DELETING"}
    assert client.get(f"/api/files/{file25}/").status_code == 404
    assert client.get(f"/api/files/{file25}/measurements/").status_code == 404
    assert not (storage.files_dir / file25).exists()
    assert client.delete(f"/api/files/{file25}/").status_code == 404


def test_health_ready_and_request_id(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok", "storage": "ok"}

    echoed = client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert echoed.headers["x-request-id"] == "trace-123"
    sanitised = client.get("/health", headers={"X-Request-ID": "bad id\r\nX: y"})
    assert " " not in sanitised.headers["x-request-id"]
    problem = client.get("/api/files/nope/", headers={"X-Request-ID": "trace-9"}).json()
    assert problem["request_id"] == "trace-9"


def test_ready_reports_unavailable_dependencies(client):
    async def broken():
        raise OSError("disk full")

    client.app.state.storage.check_writable = broken
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["storage"].startswith("error")


def test_openapi_documents_the_required_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/api/files/"]
    assert "get" in paths["/api/files/{file_id}/"]
    assert "get" in paths["/api/files/{file_id}/measurements/"]
