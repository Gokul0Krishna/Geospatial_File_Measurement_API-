"""End-to-end smoke test against a RUNNING instance (any processing mode).

    python -m scripts.smoke_test [http://localhost:8000]

Uploads the files in ./samples, waits for processing, and checks the numbers.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def wait_until_done(client: httpx.Client, file_id: str, timeout_s: float = 60) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = client.get(f"/api/files/{file_id}/").json()
        if info["status"] in {"COMPLETED", "FAILED"}:
            return info
        time.sleep(0.5)
    raise TimeoutError(f"file {file_id} did not finish within {timeout_s}s")


def wait_until_ready(client: httpx.Client, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if client.get("/ready").status_code == 200:
                return
        except httpx.TransportError:
            pass  # server still starting
        if time.monotonic() > deadline:
            raise TimeoutError("service never became ready")
        time.sleep(0.5)


def upload(client: httpx.Client, name: str) -> dict:
    with open(SAMPLES / name, "rb") as handle:
        response = client.post("/api/files/", files={"file": (name, handle)})
    assert response.status_code in {201, 202}, response.text
    print(f"POST {name:<28} -> HTTP {response.status_code} status={response.json()['status']}")
    return wait_until_done(client, response.json()["id"])


def main(base_url: str) -> int:
    with httpx.Client(base_url=base_url, timeout=30) as client:
        wait_until_ready(client)
        print("ready:", client.get("/ready").json()["checks"])

        # 1. KML with mixed content --------------------------------------------------
        kml = upload(client, "survey.kml")
        assert kml["status"] == "COMPLETED" and kml["feature_count"] == 6, kml
        print(f"  {kml['filename']}: {kml['feature_count']} features, crs={kml['crs']}, summary={kml['summary']['by_status']}")
        page = client.get(f"/api/files/{kml['id']}/measurements/").json()
        for item in page["items"]:
            m = item["measurement"]
            shown = f"{m['type']}={m['value']:,.1f} {m['unit']} in {m['crs']}" if m else item["reason"]
            print(f"    #{item['feature_index']} {str(item['geometry_type']):<18} {item['status']:<15} {shown}")
        statuses = [i["status"] for i in page["items"]]
        assert statuses == ["MEASURED", "MEASURED", "MEASURED", "NOT_APPLICABLE", "UNSUPPORTED", "EMPTY"], statuses

        # 2. Web Mercator shapefile: reported area must be the TRUE area --------------
        wm = upload(client, "parcels_web_mercator.zip")
        assert wm["status"] == "COMPLETED" and wm["crs"] == "EPSG:3857", wm
        planar = client.get(f"/api/files/{wm['id']}/features/", params={"include_geometry": "true"}).json()["items"]
        utm = client.get(f"/api/files/{wm['id']}/measurements/").json()["items"]
        geo = client.get(f"/api/files/{wm['id']}/measurements/", params={"method": "geodesic"}).json()["items"]
        for p, u, g in zip(planar, utm, geo, strict=True):
            ring = p["geometry"]["coordinates"][0]
            xs, ys = [c[0] for c in ring], [c[1] for c in ring]
            planar_3857 = abs(sum(x1 * y2 - x2 * y1 for x1, x2, y1, y2 in zip(xs, xs[1:], ys, ys[1:], strict=False))) / 2
            ratio = planar_3857 / u["measurement"]["value"]
            diff = abs(u["measurement"]["value"] - g["measurement"]["value"]) / g["measurement"]["value"]
            print(f"  plot {u['feature_index']}: utm={u['measurement']['value']:,.0f} m2 geodesic={g['measurement']['value']:,.0f} m2 "
                  f"(utm/geodesic differ {diff:.3%}); naive Web Mercator area would be {ratio:.2f}x too large")
            assert diff < 0.003 and 3.5 < ratio < 4.5

        # 3. Lines, then delete ------------------------------------------------------
        roads = upload(client, "roads_wgs84.zip")
        assert roads["status"] == "COMPLETED"
        lengths = [i["measurement"]["value"] for i in client.get(f"/api/files/{roads['id']}/measurements/").json()["items"]]
        print(f"  road lengths: {[round(v, 1) for v in lengths]} m")
        assert client.delete(f"/api/files/{roads['id']}/").status_code == 202
        assert client.get(f"/api/files/{roads['id']}/").status_code == 404
        assert client.get("/api/files/does-not-exist/measurements/").status_code == 404
        print("OK: all smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"))
