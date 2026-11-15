"""Publish the rolling six-month step calendar used on the GitHub profile."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from statistics import quantiles

import google.cloud.storage as storage

from personal_data_platform.storage.warehouse import Warehouse

_PALETTES = {
    "light": (
        "#ffffff",
        "#d1d9e0",
        "#1f2328",
        "#59636e",
        "#ebedf0",
        "#9be9a8",
        "#40c463",
        "#30a14e",
        "#216e39",
    ),
    "dark": (
        "#0d1117",
        "#3d444d",
        "#f0f6fc",
        "#9198a1",
        "#212830",
        "#0e4429",
        "#006d32",
        "#26a641",
        "#39d353",
    ),
}


def publish(warehouse: Warehouse, end: date, bucket_name: str, *, timeout: int) -> None:
    today = end + timedelta(days=1)
    year, month_index = divmod(today.year * 12 + today.month - 1 - 6, 12)
    month = month_index + 1
    start = date(year, month, min(today.day, monthrange(year, month)[1]))
    steps: dict[date, float | None] = dict(
        warehouse.query_rows(
            "SELECT activity_date, steps FROM marts.daily_steps WHERE activity_date BETWEEN ? AND ?",
            [start, end],
        )
    )
    positive = sorted(count for count in steps.values() if count is not None and count > 0)
    maximum = max(positive, default=0.0)
    if len(positive) >= 4:
        lower, _, upper = quantiles(positive, method="inclusive")
        fence = upper + 1.5 * (upper - lower)
        maximum = max(count for count in positive if count <= fence)
    bounds = (maximum * 0.25, maximum * 0.50, maximum * 0.75)

    with storage.Client() as client:
        bucket = client.bucket(bucket_name)
        for theme in _PALETTES:
            blob = bucket.blob(f"daily-steps-{theme}.svg")
            blob.cache_control = "public, max-age=3600"
            blob.upload_from_string(
                _render(start, end, steps, bounds, theme),
                content_type="image/svg+xml",
                timeout=timeout,
            )


def _label(x: int, y: int, text: str, cls: str = "label") -> str:
    return f'<text x="{x}" y="{y}" class="{cls}">{text}</text>'


def _square(x: int, y: int, size: int, cls: str) -> str:
    return f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="2.5" class="{cls}"/>'


def _render(
    start: date,
    end: date,
    steps: dict[date, float | None],
    bounds: tuple[float, float, float],
    theme: str,
) -> str:
    days = (end - start).days + 1
    offset = (start.weekday() + 1) % 7
    weeks = (days + offset + 6) // 7
    width = 62 + weeks * 18 + 14
    start_label = f"{start:%b} {start.day}"
    if start.year != end.year:
        start_label += f", {start.year}"
    period = f"{start_label} – {end:%b} {end.day}, {end.year} · JST"

    background, border, text, muted, zero, *greens = _PALETTES[theme]
    styles = "\n".join(
        [
            'svg {font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}',
            f".card {{fill:{background};stroke:{border}}}",
            f".title {{fill:{text};font-size:18px;font-weight:600}}",
            f".label {{fill:{muted};font-size:11px}}",
            f".zero {{fill:{zero}}}",
            *(f".g{i} {{fill:{color}}}" for i, color in enumerate(greens, 1)),
        ]
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="242" '
        f'viewBox="0 0 {width} 242" role="img" aria-labelledby="title description">',
        '<title id="title">Daily steps</title>',
        f'<desc id="description">{period}. Each column is a week starting on Sunday.</desc>',
        f"<style>\n{styles}\n</style>",
        f'<rect class="card" x=".5" y=".5" width="{width - 1}" height="241" rx="8"/>',
        _label(18, 29, "Daily steps", "title"),
        _label(18, 48, period),
    ]
    for row, name in enumerate(("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")):
        parts.append(_label(22, 95 + row * 18, name))

    next_month_column = (offset + monthrange(start.year, start.month)[1] - start.day + 1) // 7
    for i in range(days):
        day = start + timedelta(days=i)
        column, row = divmod(i + offset, 7)
        x, y = 62 + column * 18, 84 + row * 18
        if day.day == 1 or (i == 0 and next_month_column >= 2):
            parts.append(_label(x, 74, f"{day:%b}"))
        count = steps.get(day)
        if count is not None:
            color = "zero" if count == 0 else f"g{1 + sum(count >= bound for bound in bounds)}"
            parts.append(_square(x, y, 14, color))

    key_x = width - 142
    parts.append(_label(key_x, 222, "Less"))
    parts.extend(_square(key_x + 30 + i * 17, 211, 13, f"g{i + 1}") for i in range(4))
    parts.extend([_label(key_x + 101, 222, "More"), "</svg>"])
    return "\n".join(parts) + "\n"
