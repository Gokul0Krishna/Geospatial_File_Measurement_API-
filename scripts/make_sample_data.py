"""Regenerate the files in ./samples (needs the dev extras: `pip install -e ".[dev]"`).

    python -m scripts.make_sample_data
"""

from __future__ import annotations

from pathlib import Path

from pyproj import CRS, Transformer

from tests.helpers import (
    BLR_HOLE,
    BLR_SQUARE,
    build_shapefile_zip,
    kml_document,
    kml_line,
    kml_placemark,
    kml_point,
    kml_polygon,
)

OUT = Path(__file__).resolve().parent.parent / "samples"


def survey_kml() -> bytes:
    second = [(77.610, 12.970), (77.620, 12.970), (77.620, 12.980), (77.610, 12.980)]
    third = [(77.630, 12.970), (77.640, 12.970), (77.640, 12.980)]
    multi = f"<MultiGeometry>{kml_polygon(second)}{kml_polygon(third)}</MultiGeometry>"
    mixed = f"<MultiGeometry>{kml_point(77.5, 12.9)}{kml_line([(77.5, 12.9), (77.6, 12.95)])}</MultiGeometry>"
    return kml_document(
        (
            "Parcels",
            [
                kml_placemark("Plot A (with courtyard)", kml_polygon(BLR_SQUARE, [BLR_HOLE]), {"owner": "Alice", "zone": "R1"}),
                kml_placemark("Plot B (two parts)", multi, {"owner": "Bob", "zone": "R2"}),
            ],
        ),
        ("Roads", [kml_placemark("MG Road", kml_line([(77.59, 12.97), (77.60, 12.99), (77.62, 12.99)]))]),
        (
            "Other",
            [
                kml_placemark("Water well", kml_point(77.595, 12.975)),
                kml_placemark("Mixed (point + line)", mixed),
                kml_placemark("Placemark without geometry"),
            ],
        ),
    )


def web_mercator_parcels() -> bytes:
    """Helsinki (60N) in EPSG:3857, where Web Mercator inflates areas about 4x."""
    to_3857 = Transformer.from_crs(4326, 3857, always_xy=True)
    plots = [
        [(24.900, 60.170), (24.910, 60.170), (24.910, 60.175), (24.900, 60.175)],
        [(24.920, 60.170), (24.930, 60.170), (24.930, 60.176), (24.920, 60.176)],
    ]
    shapes = [[[to_3857.transform(x, y) for x, y in ring]] for ring in plots]
    return build_shapefile_zip(
        "polygon", shapes, crs=CRS.from_epsg(3857), records=[("Harbour plot", 1.0), ("Market plot", 2.0)]
    )


def roads_wgs84() -> bytes:
    lines = [
        [(77.59, 12.97), (77.60, 12.99)],
        [(77.60, 12.99), (77.62, 12.99), (77.63, 13.00)],
    ]
    return build_shapefile_zip("line", lines, records=[("Road 1", 1.0), ("Road 2", 2.0)])


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    (OUT / "survey.kml").write_bytes(survey_kml())
    (OUT / "parcels_web_mercator.zip").write_bytes(web_mercator_parcels())
    (OUT / "roads_wgs84.zip").write_bytes(roads_wgs84())
    print("wrote", ", ".join(sorted(p.name for p in OUT.iterdir())))
