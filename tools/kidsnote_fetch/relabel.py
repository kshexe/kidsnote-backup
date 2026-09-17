"""Rewrite label text on already-published report pages, in place (--relabel-existing).

Why: on 2026-09-14 the label wording switched to kidsnote's own wording, and
codes such as `watery` that used to be printed raw got labels. Re-publishing
would give every page a new link and drop its comments, so instead only the
text this tool itself generated from kidsnote codes is rewritten:

  * the life-record chips on the gray meta line ("💩 배변 watery" -> "💩 배변 묽음")
  * the "오늘의 날씨: ..." callout
  * the lines under "💩 배변 기록"

A block is rewritten only when its text is exactly what an earlier version of
this tool would have rendered from the same kidsnote data: the previous
wording, or the raw code for entries the tables lacked back then. Anything
else (a renamed teacher, a hand edit, a formatted block) is left untouched and
counted as skipped. A second run finds every page already current.

Logs carry counts only, never page text: Actions logs of a public fork are public.
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import notion_mirror as nm

_LOGGER = logging.getLogger(__name__)
_sleep = time.sleep  # swapped out in tests

# Label tables as of commit 8369cb6, the last version before the wording was
# aligned with kidsnote's UI. Earlier versions only lacked some of these
# entries (git history shows additions only); those rendered as the raw code,
# which the "raw" rendering covers.
LEGACY_SLEEP_HOUR_KO = {
    "no_sleep": "안 잤음",
    "none": "안 잠",
    "below_1": "1시간 미만",
    "under_30m": "30분 이내",
    "30m_to_1": "30분~1시간",
    "1_to_1.5": "1~1.5시간",
    "1.5_to_2": "1.5~2시간",
    "over_2": "2시간 이상",
}
LEGACY_STATUS_KO = {
    "good": "좋음",
    "average": "보통",
    "bad": "안 좋음",
    "normal": "정상",
    "high": "높음",
    "low": "낮음",
    "soft": "묽음",
    "hard": "딱딱",
    "none": "없음",
    "fixed": "정해진 식단",
    "more": "많이 먹음",
    "less": "적게 먹음",
    "sick": "아픔",
    "fine": "양호",
    "trimmed": "정리됨",
    "needs_trim": "정리 필요",
    "active": "활발",
    "calm": "차분",
}
LEGACY_WEATHER_KO = {
    "sunny": "☀️ 맑음",
    "partly_cloudy": "⛅ 구름 조금",
    "mostly_cloudy": "🌥️ 구름 많음",
    "overcast": "☁️ 흐림",
    "fog": "🌫️ 안개",
    "rain": "🌧️ 비",
    "sunny_after_rain": "🌈 비온 뒤 맑음",
    "snow": "❄️ 눈",
    "yellow_sand": "🟡 황사",
    "thunderstorm": "⛈️ 천둥번개",
    "mixed_rain_snow": "🌨️ 진눈깨비",
    "cloudy": "☁️ 흐림",
    "rainy": "🌧️ 비",
    "snowy": "❄️ 눈",
    "foggy": "🌫️ 안개",
    "windy": "💨 바람",
    "stormy": "⛈️ 폭풍",
    "hot": "🥵 더움",
    "cold": "🥶 추움",
}

WEATHER_PREFIX = "오늘의 날씨: "
BOWEL_HEADING = "💩 배변 기록"
META_SEPARATOR = " · "
MAX_ATTEMPTS = 5


@contextlib.contextmanager
def _label_tables(status: dict, sleep: dict, weather: dict, activity: dict) -> Iterator[None]:
    """Render notion_mirror's builders with other label tables for a moment."""
    saved = (nm.STATUS_KO, nm.SLEEP_HOUR_KO, nm.WEATHER_KO, nm.ACTIVITY_RATE_KO)
    nm.STATUS_KO, nm.SLEEP_HOUR_KO, nm.WEATHER_KO, nm.ACTIVITY_RATE_KO = status, sleep, weather, activity
    try:
        yield
    finally:
        nm.STATUS_KO, nm.SLEEP_HOUR_KO, nm.WEATHER_KO, nm.ACTIVITY_RATE_KO = saved


def _block_text(block: dict[str, Any]) -> str:
    body = block.get(block.get("type") or "") or {}
    parts = []
    for seg in body.get("rich_text") or []:
        text = seg.get("plain_text")
        parts.append(text if text is not None else (seg.get("text") or {}).get("content", ""))
    return "".join(parts)


def _bowel_lines(blocks: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    inside = False
    for b in blocks:
        if b.get("type") == "heading_3":
            if inside:
                break
            inside = _block_text(b) == BOWEL_HEADING
        elif inside and b.get("type") == "paragraph":
            lines.append(_block_text(b))
    return lines


@dataclass
class Rendering:
    chips: list[str]
    weather: str | None
    bowel_lines: list[str]


def render(report: dict[str, Any], variant: str) -> Rendering:
    """Page text for ``report`` with the current ("new"), previous ("legacy") or no ("raw") labels."""
    if variant == "new":
        tables = (nm.STATUS_KO, nm.SLEEP_HOUR_KO, nm.WEATHER_KO, nm.ACTIVITY_RATE_KO)
    elif variant == "legacy":
        tables = (LEGACY_STATUS_KO, LEGACY_SLEEP_HOUR_KO, LEGACY_WEATHER_KO, {})
    elif variant == "raw":
        tables = ({}, {}, {}, {})
    else:
        raise ValueError(f"unknown variant {variant!r}")
    with _label_tables(*tables):
        chips = nm.NotionMirror._life_record_bits(report)
        detail = nm.NotionMirror._life_record_detail_blocks(report)
    author_type = (report.get("author") or {}).get("type") or ""
    code = report.get("weather") if author_type != "parent" else None
    if not code or (variant == "new" and code in nm.WEATHER_HIDDEN):
        weather = None  # older versions showed hidden codes raw; the current one shows nothing
    else:
        weather = WEATHER_PREFIX + tables[2].get(code, code)
    return Rendering(chips, weather, _bowel_lines(detail))


@dataclass
class PagePlan:
    updates: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (block id, PATCH body)
    deletes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # parts left alone: meta / weather / bowel

    @property
    def changed(self) -> bool:
        return bool(self.updates or self.deletes)


def _rewritable(block: dict[str, Any]) -> bool:
    """Only single plain-text segments are ours to rewrite; formatting or links mean a person touched it."""
    segs = (block.get(block.get("type") or "") or {}).get("rich_text") or []
    if len(segs) != 1 or segs[0].get("type") != "text":
        return False
    if (segs[0].get("text") or {}).get("link"):
        return False
    ann = segs[0].get("annotations") or {}
    return not any(ann.get(k) for k in ("bold", "italic", "strikethrough", "underline", "code"))


def _patch_body(block: dict[str, Any], content: str) -> dict[str, Any]:
    btype = block["type"]
    old = (block[btype].get("rich_text") or [{}])[0]
    seg: dict[str, Any] = {"type": "text", "text": {"content": content}}
    if old.get("annotations"):
        seg["annotations"] = old["annotations"]  # keeps the gray meta-line color
    return {btype: {"rich_text": [seg]}}


def plan_page(blocks: list[dict[str, Any]], report: dict[str, Any]) -> PagePlan:
    """Decide which blocks of a published report page to rewrite. Pure: no network."""
    plan = PagePlan()
    new, legacy, raw = render(report, "new"), render(report, "legacy"), render(report, "raw")

    # 1. Life-record chips at the end of the gray meta line (the page's first block).
    meta = nm.NotionMirror._meta_bits(report)
    if meta and new.chips and blocks and blocks[0].get("type") == "paragraph":
        first = blocks[0]
        parts = _block_text(first).split(META_SEPARATOR)
        old_chips = parts[len(meta):]
        if old_chips != new.chips or parts[:len(meta)] != meta:
            known = (
                parts[:len(meta)] == meta
                and len(old_chips) == len(new.chips)
                and all(chip in (n, l, r) for chip, n, l, r in
                        zip(old_chips, new.chips, legacy.chips, raw.chips))
            )
            if known and _rewritable(first):
                plan.updates.append((first["id"], _patch_body(first, META_SEPARATOR.join(meta + new.chips))))
            elif old_chips != new.chips:
                plan.skipped.append("meta")

    # 2. "오늘의 날씨: ..." callout.
    weather_block = next((b for b in blocks if b.get("type") == "callout"
                          and _block_text(b).startswith(WEATHER_PREFIX)), None)
    if weather_block is not None:
        text = _block_text(weather_block)
        if text != new.weather:
            if text in {legacy.weather, raw.weather} and _rewritable(weather_block):
                if new.weather is None:
                    plan.deletes.append(weather_block["id"])
                else:
                    plan.updates.append((weather_block["id"], _patch_body(weather_block, new.weather)))
            else:
                plan.skipped.append("weather")

    # 3. Lines under "💩 배변 기록".
    if new.bowel_lines:
        start = next((i for i, b in enumerate(blocks) if b.get("type") == "heading_3"
                      and _block_text(b) == BOWEL_HEADING), None)
        if start is not None:
            rows = blocks[start + 1:start + 1 + len(new.bowel_lines)]
            row_texts = [_block_text(b) for b in rows]
            if row_texts != new.bowel_lines:
                known = (
                    len(rows) == len(new.bowel_lines)
                    and all(b.get("type") == "paragraph" for b in rows)
                    and all(t in (n, l, r) for t, n, l, r in
                            zip(row_texts, new.bowel_lines, legacy.bowel_lines, raw.bowel_lines))
                    and all(_rewritable(b) for b, t, n in zip(rows, row_texts, new.bowel_lines) if t != n)
                )
                if known:
                    for b, t, n in zip(rows, row_texts, new.bowel_lines):
                        if t != n:
                            plan.updates.append((b["id"], _patch_body(b, n)))
                else:
                    plan.skipped.append("bowel")
    return plan


def _call(mirror: Any, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """One Notion API call, retrying 429 / 5xx with backoff."""
    for attempt in range(MAX_ATTEMPTS):
        r = mirror.session.request(method, url, headers=mirror._headers(), timeout=mirror.timeout, **kwargs)
        if (r.status_code == 429 or r.status_code >= 500) and attempt < MAX_ATTEMPTS - 1:
            try:
                wait = float(r.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0.0
            _sleep(min(max(wait, 2 ** attempt), 30))
            continue
        r.raise_for_status()
        return r.json() if r.content else {}
    raise AssertionError("unreachable")


def _page_blocks(mirror: Any, page_id: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = _call(mirror, "GET", f"{nm.NOTION_API}/blocks/{page_id}/children", params=params)
        blocks.extend(data.get("results") or [])
        if not data.get("has_more"):
            return blocks
        cursor = data.get("next_cursor")


def relabel_report_pages(
    mirror: Any,
    reports: list[dict[str, Any]],
    page_map: dict[int, str],
    *,
    time_left: Callable[[], float] | None = None,
) -> dict[str, int]:
    """Rewrite outdated label text on every published page among ``reports``. Returns counts."""
    counts: Counter[str] = Counter(updated=0, unchanged=0, skipped=0, failed=0)
    for report in reports:
        try:
            report_id = int(report.get("id") or 0)
        except (TypeError, ValueError):
            continue
        page_id = page_map.get(report_id)
        if not page_id:
            continue
        if time_left is not None and time_left() <= 0:
            counts["stopped_early"] += 1
            _LOGGER.warning("🏷️ Relabel: time budget reached; run again with relabel=on to finish")
            break
        try:
            plan = plan_page(_page_blocks(mirror, page_id), report)
            for block_id, body in plan.updates:
                _call(mirror, "PATCH", f"{nm.NOTION_API}/blocks/{block_id}", json=body)
            for block_id in plan.deletes:
                _call(mirror, "DELETE", f"{nm.NOTION_API}/blocks/{block_id}")
        except Exception as e:  # one bad page must not stop the rest
            counts["failed"] += 1
            status = getattr(getattr(e, "response", None), "status_code", "")
            _LOGGER.warning("🏷️ Relabel failed for report id=%s: %s %s", report_id, type(e).__name__, status)
            continue
        if plan.changed:
            counts["updated"] += 1
        elif plan.skipped:
            counts["skipped"] += 1
        else:
            counts["unchanged"] += 1
        for part in plan.skipped:
            counts[f"skipped_{part}"] += 1
    return dict(counts)
