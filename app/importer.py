"""CSV import parsing with row-level error collection."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from . import db

REQUIRED = ("grid", "exposure", "night", "utc", "band")
ALIASES = {
    "grid": ("grid", "grid_id", "网格号", "格号"),
    "exposure": ("exposure", "exposure_no", "exp", "曝光号"),
    "night": ("night", "night_id", "观测夜", "观测夜编号"),
    "utc": ("utc", "utc_time", "time", "utc时刻", "时刻"),
    "band": ("band", "filter", "波段"),
}


@dataclass
class ParseResult:
    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.rows)


def _resolve_header(header: list[str]) -> dict[str, int]:
    cleaned = [h.strip().lower() for h in header]
    mapping: dict[str, int] = {}
    for fieldname, aliases in ALIASES.items():
        for idx, name in enumerate(cleaned):
            if name in aliases:
                mapping[fieldname] = idx
                break
    return mapping


def parse_csv(text: str) -> ParseResult:
    result = ParseResult()
    if not text.strip():
        result.errors.append("CSV 为空")
        return result
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        result.errors.append("CSV 无表头")
        return result
    mapping = _resolve_header(header)
    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        result.errors.append(
            "缺少列: " + ", ".join(missing) + f"（识别到表头 {header!r}）"
        )
        return result

    seen_exposures: dict[tuple[str, str], tuple[int, str, str]] = {}
    seen_links: set[tuple[str, str, str]] = set()
    for line_no, raw in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw):
            continue
        if len(raw) < len(header):
            raw = raw + [""] * (len(header) - len(raw))
        try:
            grid_id = raw[mapping["grid"]].strip()
            exposure_no = raw[mapping["exposure"]].strip()
            night = raw[mapping["night"]].strip()
            utc_time = db.parse_utc(raw[mapping["utc"]])
            band = raw[mapping["band"]].strip().lower()
        except ValueError as exc:
            result.errors.append(f"第 {line_no} 行: {exc}")
            continue
        if not grid_id or not exposure_no or not night:
            result.errors.append(f"第 {line_no} 行: 网格号/曝光号/观测夜不能为空")
            continue
        if band not in ("g", "r"):
            result.errors.append(f"第 {line_no} 行: 波段必须是 g 或 r，实际 {band!r}")
            continue
        exp_key = (night, exposure_no)
        previous = seen_exposures.get(exp_key)
        if previous is not None:
            prev_line, prev_band, prev_utc = previous
            if (prev_band, prev_utc) != (band, utc_time):
                result.errors.append(
                    f"第 {line_no} 行: 曝光 {night}/{exposure_no} 与第 {prev_line} 行"
                    f"的波段或时刻不一致"
                )
                continue
        else:
            seen_exposures[exp_key] = (line_no, band, utc_time)
        link_key = (night, exposure_no, grid_id)
        if link_key in seen_links:
            result.errors.append(
                f"第 {line_no} 行: 曝光 {night}/{exposure_no} 对网格 {grid_id} 的覆盖重复"
            )
            continue
        seen_links.add(link_key)
        result.rows.append(
            {
                "grid_id": grid_id,
                "exposure_no": exposure_no,
                "night": night,
                "utc_time": utc_time,
                "band": band,
                "line": line_no,
            }
        )
    if not result.rows and not result.errors:
        result.errors.append("CSV 没有数据行")
    return result
