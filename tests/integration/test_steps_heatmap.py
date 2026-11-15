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
    monkeypatch.setattr(steps_heatmap, "Client", Client)
    with connect(WarehouseConfig(":memory:")) as connection:
        connection.execute("CREATE SCHEMA marts")
        connection.execute("""
            CREATE TABLE marts.daily_steps AS
            SELECT date::DATE AS activity_date, steps::DOUBLE AS steps
            FROM (VALUES
                ('2025-11-14', 999), ('2025-11-15', 0), ('2025-11-17', NULL),
                ('2025-11-18', 2), ('2025-11-19', 4), ('2025-11-20', 6),
                ('2025-11-21', 8), ('2026-11-14', 100), ('2026-11-15', 999)
            ) rows(date, steps)
        """)
        steps_heatmap.publish(Warehouse(connection), date(2026, 11, 14), "profile", timeout=10)

    assert set(uploads) == {"daily-steps-light.svg", "daily-steps-dark.svg"}
    for theme, background, palette in (
        ("light", "#ffffff", ["#eff2f5", "#4ac26b", "#2da44e", "#116329"]),
        ("dark", "#0d1117", ["#151b23", "#196c2e", "#2ea043", "#56d364"]),
    ):
        document, content_type, cache_control = uploads[f"daily-steps-{theme}.svg"]
        assert content_type == "image/svg+xml"
        assert cache_control == "public, max-age=3600"
        svg = ElementTree.fromstring(document)
        assert svg.get("width") == "856"
        assert svg.find(f"{SVG}rect").get("fill") == background
        cells = svg.findall(f'{SVG}g/{SVG}rect[@width="11"]')
        assert [(cell.get("x"), cell.get("y"), cell.get("fill")) for cell in cells] == [
            ("48", "146", palette[0]),
            ("63", "86", palette[1]),
            ("63", "101", palette[2]),
            ("63", "116", palette[3]),
            ("63", "131", palette[3]),
            ("828", "146", palette[3]),
        ]
        labels = [n.text for n in svg.findall(f"{SVG}g/{SVG}text")]
        assert [label for label in labels if label in ("Mon", "Wed", "Fri")] == [
            "Mon",
            "Wed",
            "Fri",
        ]
        assert "Nov 15, 2025 – Nov 14, 2026 · JST" in labels
        assert "No data" not in document and "0 steps" not in document
