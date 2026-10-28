from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from growthstack.web import charts

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["sparkline"] = charts.sparkline
templates.env.globals["bars"] = charts.bars


def _pct(value) -> str:
    return "-" if value is None else f"{value:.0f}"


templates.env.filters["pct"] = _pct
templates.env.filters["num"] = lambda v: "-" if v is None else f"{v:,.0f}"
templates.env.filters["dt"] = lambda v: v.strftime("%d %b %H:%M") if v else "-"
