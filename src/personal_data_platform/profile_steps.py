"""Publish the rolling ten-month step calendar used on the GitHub profile."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from statistics import quantiles

from google.cloud.storage import Client

from personal_data_platform.storage.warehouse import Warehouse

_PALETTES = {
    "light": (
        "#ffffff",
        "#d1d9e0",
        "#1f2328",
        "#59636e",
        "#1f2328",
        "#eff2f5",
        "#aceebb",
        "#4ac26b",
        "#2da44e",
        "#116329",
    ),
    "dark": (
        "#0d1117",
        "#3d444d",
        "#f0f6fc",
        "#9198a1",
        "#010409",
        "#151b23",
        "#033a16",
        "#196c2e",
        "#2ea043",
        "#56d364",
    ),
}


def publish(warehouse: Warehouse, end: date, bucket_name: str, *, timeout: int) -> None:
    today = end + timedelta(days=1)
    year, month_index = divmod(today.year * 12 + today.month - 1 - 10, 12)
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

    with Client() as client:
        bucket = client.bucket(bucket_name)
        for theme in _PALETTES:
            blob = bucket.blob(f"daily-steps-{theme}.svg")
            blob.cache_control = "public, max-age=3600"
            blob.upload_from_string(
                _render(start, end, steps, bounds, theme),
                content_type="image/svg+xml",
                timeout=timeout,
            )


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
    width = 48 + (weeks - 1) * 15 + 11 + 17
    start_label = f"{start:%b} {start.day}"
    if start.year != end.year:
        start_label += f", {start.year}"
    period = f"{start_label} – {end:%b} {end.day}, {end.year} · JST"
    background, border, ink, muted, stroke, *colors = _PALETTES[theme]

    def label(x: int, y: int, value: str, *, color: str = ink, size: int = 12) -> str:
        return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}">{value}</text>'

    def square(x: int, y: int, size: int, color: str) -> str:
        return (
            f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="2" '
            f'fill="{color}" stroke="{stroke}" stroke-width=".5" stroke-opacity=".05"/>'
        )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="189" '
        f'viewBox="0 0 {width} 189" role="img" aria-labelledby="title">',
        f'<title id="title">Daily steps · {period}</title>',
        f'<rect width="{width}" height="189" fill="{background}"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        label(0, 16, "Daily steps", size=16),
        f'<rect x=".5" y="27.5" width="{width - 1}" height="161" rx="6" '
        f'fill="{background}" stroke="{border}"/>',
    ]
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        parts.append(label(16, 66 + row * 15, name))

    next_month_column = (offset + monthrange(start.year, start.month)[1] - start.day + 1) // 7
    for i in range(days):
        day = start + timedelta(days=i)
        column, row = divmod(i + offset, 7)
        x, y = 48 + column * 15, 56 + row * 15
        if day.day == 1 or (i == 0 and next_month_column >= 2):
            parts.append(label(x, 49, f"{day:%b}"))
        count = steps.get(day)
        if count is not None:
            level = 0 if count == 0 else 1 + sum(count >= bound for bound in bounds)
            parts.append(square(x, y, 11, colors[level]))

    key_x = width - 135
    parts.extend([label(16, 176, period, color=muted), label(key_x, 176, "Less", color=muted)])
    parts.extend(square(key_x + 28 + i * 14, 167, 10, color) for i, color in enumerate(colors[1:]))
    parts.extend([label(key_x + 84, 176, "More", color=muted), "</g>", "</svg>"])
    return "\n".join(parts) + "\n"
