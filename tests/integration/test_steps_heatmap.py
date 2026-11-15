from datetime import date
from xml.etree import ElementTree

from personal_data_platform.reconciliation import steps_heatmap
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

SVG = "{http://www.w3.org/2000/svg}"


def test_publishes_both_themes_with_the_calendar_window(monkeypatch):
    class Blob:
        def __init__(self, name):
            self.name = name

        def upload_from_string(self, data, *, content_type, timeout):
            uploads[self.name] = data, content_type, self.cache_control

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bucket(self, name):
            assert name == "profile"
            return self

        def blob(self, name):
            return Blob(name)

    uploads = {}
    monkeypatch.setattr(steps_heatmap.storage, "Client", Client)
    with connect(WarehouseConfig(":memory:")) as connection:
        connection.execute("CREATE SCHEMA marts")
        connection.execute("""
            CREATE TABLE marts.daily_steps AS
            SELECT date::DATE AS activity_date, steps::DOUBLE AS steps
            FROM (VALUES
                ('2026-05-14', 999), ('2026-05-15', 0), ('2026-05-17', NULL),
                ('2026-05-18', 2), ('2026-05-19', 4), ('2026-05-20', 6),
                ('2026-05-21', 8), ('2026-11-14', 100), ('2026-11-15', 999)
            ) rows(date, steps)
        """)
        steps_heatmap.publish(Warehouse(connection), date(2026, 11, 14), "profile", timeout=10)

    assert set(uploads) == {"daily-steps-light.svg", "daily-steps-dark.svg"}
    for theme, background in [("light", "#ffffff"), ("dark", "#0d1117")]:
        document, content_type, cache_control = uploads[f"daily-steps-{theme}.svg"]
        assert content_type == "image/svg+xml"
        assert cache_control == "public, max-age=3600"
        svg = ElementTree.fromstring(document)
        cells = svg.findall(f'{SVG}rect[@width="14"]')
        assert [(cell.get("x"), cell.get("y"), cell.get("class")) for cell in cells] == [
            ("62", "174", "zero"),
            ("80", "102", "g2"),
            ("80", "120", "g3"),
            ("80", "138", "g4"),
            ("80", "156", "g4"),
            ("530", "192", "g4"),
        ]
        assert background in svg.find(f"{SVG}style").text
        assert "May 15 – Nov 14, 2026 · JST" in document
        assert "No data" not in document and "0 steps" not in document
