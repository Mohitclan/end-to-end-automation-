"""Tiny server-rendered SVG charts, so the dashboard needs no JavaScript or CDN.
Colours come from CSS custom properties, so the charts follow light/dark mode."""

from __future__ import annotations

from html import escape

from markupsafe import Markup


def sparkline(values: list[float], *, width: int = 280, height: int = 56, label: str = "") -> Markup:
    if not values:
        return Markup('<div class="empty-chart">No data yet</div>')
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = width / max(1, len(values) - 1)
    pts = [(i * step, height - 4 - (v - lo) / span * (height - 8)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"0,{height} {line} {width},{height}"
    lx, ly = pts[-1]
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}" '
        f'preserveAspectRatio="none">'
        f'<polygon points="{area}" fill="var(--chart-fill)"/>'
        f'<polyline points="{line}" fill="none" stroke="var(--chart-line)" stroke-width="2" '
        f'vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="var(--chart-line)"/></svg>'
    )


def bars(items: list[tuple[str, float]], *, max_value: float | None = None) -> Markup:
    """Horizontal bars as HTML rows: [(label, value), ...]."""
    if not items:
        return Markup('<div class="empty-chart">No data yet</div>')
    top = max_value or max(v for _, v in items) or 1
    rows = []
    for label, value in items:
        pct = max(0.0, min(100.0, value / top * 100))
        rows.append(
            f'<div class="bar-row"><span class="bar-label">{escape(label)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{pct:.1f}%"></span></span>'
            f'<span class="bar-value">{value:,.0f}</span></div>'
        )
    return Markup("".join(rows))
