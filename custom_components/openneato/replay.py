"""Session parsing for the interactive replay card.

Direct port of `frontend/src/history-data.ts::buildSession`, with two
differences the canvas player depends on:

  * pose timestamps are normalised against the first retained pose, so the
    scrubber's 0 lines up with the summary duration;
  * recharge markers (which carry no `ts` of their own) are paired with the
    long gaps in the pose timeline, giving each one a start/end window for
    the scrubber's charge segments.

Output is deliberately compact -- flat number arrays instead of dicts, and
coordinates rounded -- because a full session ships over the WebSocket
connection to the browser.
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
from typing import Any

from PIL import Image, ImageDraw

from .const import (
    HISTORY_BG_COLOR,
    HISTORY_CELL_SIZE_M,
    HISTORY_COVERAGE_COLOR,
    HISTORY_END_COLOR,
    HISTORY_GRID_COLOR,
    HISTORY_GRID_STEP_M,
    HISTORY_IMAGE_SIZE,
    HISTORY_PAD_PX,
    HISTORY_PATH_COLOR,
    HISTORY_ROBOT_DIAMETER_M,
    HISTORY_START_COLOR,
)

_LOGGER = logging.getLogger(__name__)

# Corrupted-pose recovery. Session files are written by the ESP32 straight to
# SPIFFS, and a power cut mid-write leaves a truncated or garbled line; these
# two helpers salvage what they can instead of dropping the whole session.
# They used to live in history_renderer, which was deleted along with the
# PNG/GIF camera platform the replay card replaced.
_POSE_RE = re.compile(
    r'^\{.x.:\s*(-?[\d.eE:"\w-]+)\s*,.y.:\s*(-?[\d.eE:"\w-]+)'
    r'\s*,.t.:\s*([\d.eE:"\w-]+)\s*,.ts.:\s*([\d.eE:"\w-]+)\s*\}$'
)


def _repair_number(raw: str) -> float | None:
    """Replace non-numeric garbage with dots, collapse, parse."""
    cleaned = re.sub(r"[^0-9.eE-]", ".", raw)
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = parts[0] + "." + "".join(parts[1:])
    try:
        n = float(cleaned)
        return n if math.isfinite(n) else None
    except ValueError:
        return None


def _try_repair_pose(line: str) -> dict[str, float] | None:
    """Attempt regex-based recovery of a corrupted pose line."""
    m = _POSE_RE.match(line)
    if not m:
        return None
    vals = [_repair_number(m.group(i)) for i in range(1, 5)]
    if any(v is None for v in vals):
        return None
    return {"x": vals[0], "y": vals[1], "t": vals[2], "ts": vals[3]}

# Brush speed above which the robot counts as cleaning. Measured on a Botvac
# D6: ~1400 rpm running, exactly 0 stopped, so anything in between is a
# spin-up. Kept as a threshold rather than "> 0" so a coasting brush does not
# register as a cleaned stripe.
BRUSH_RUNNING_RPM = 200

# Firmware samples poses every ~2s; anything past 15x that is a real pause
# (docking, charging) rather than jitter between snapshots.
POSE_INTERVAL_S = 2.0
GAP_MIN_S = 30.0


def _r(value: float, digits: int = 3) -> float:
    """Round for the wire -- sub-millimetre precision is noise here."""
    return round(float(value), digits)


def build_replay_session(
    raw: str, name: str = "", align: tuple[int, int, int] | None = None
) -> dict[str, Any]:
    """Parse raw session JSONL into the payload the replay card consumes."""
    lines = [l for l in raw.strip().split("\n") if l.strip()]

    session: dict | None = None
    summary: dict | None = None
    poses: list[dict[str, float]] = []
    # Recharge markers carry no timestamp, so remember the ts of the last
    # pose seen before each one -- that anchors it on the timeline.
    raw_recharges: list[dict[str, float]] = []
    last_pose_ts = 0.0

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            repaired = _try_repair_pose(line)
            if repaired is None:
                continue
            obj = repaired

        obj_type = obj.get("type")
        if obj_type == "session":
            session = obj
        elif obj_type == "summary":
            summary = obj
        elif obj_type == "recharge":
            raw_recharges.append(
                {
                    "x": float(obj.get("x", 0)),
                    "y": float(obj.get("y", 0)),
                    "prevTs": last_pose_ts,
                }
            )
        elif "x" in obj and "y" in obj and "type" not in obj:
            last_pose_ts = float(obj.get("ts", 0))
            # Skip the origin pose (all zeros) -- same filter as the frontend.
            if obj.get("x") != 0 or obj.get("y") != 0 or obj.get("t") != 0:
                poses.append(obj)

    if not poses:
        return {
            "name": name,
            "session": session,
            "summary": summary,
            "cellSize": HISTORY_CELL_SIZE_M,
            "bounds": None,
            "duration": 0.0,
            "path": [],
            "coverage": [],
            "recharges": [],
        }

    # `ts` is seconds since boot, not since session start. Normalise so the
    # timeline starts at 0 and matches the summary duration.
    t_origin = float(poses[0].get("ts", 0))
    norm: list[tuple[float, float, float, float]] = [
        (
            float(p.get("x", 0)),
            float(p.get("y", 0)),
            float(p.get("t", 0)),
            max(0.0, float(p.get("ts", 0)) - t_origin),
        )
        for p in poses
    ]
    # Put the run in the same frame as the accumulated map.
    #
    # A session is recorded in whatever frame the robot's localisation was in
    # at the time, and that frame moves: after losing its position the robot
    # re-zeroes, and the next run comes back a quarter turn round. The map
    # corrects for this when merging, but the replay used to be served raw —
    # so the walls were straight and the cleaned area was rotated against
    # them. Measured on one such session: the wall envelope was 107x143 cells
    # and the coverage 136x101, and turning it a quarter took containment
    # from 86.8% to 99.9%.
    #
    # Applied here, before bounds, coverage and recharges are derived, so all
    # of them come out in the corrected frame without each needing to know.
    if align and any(align):
        norm = _apply_alignment(norm, align)

    recharges = _pair_recharges(raw_recharges, norm, t_origin)
    # `b` is the brush speed the firmware records with each pose; absent on
    # sessions from before it did, in which case every pose counts as cleaning.
    brush = [p.get("b") for p in poses]
    coverage = _coverage_cells(norm, brush)

    pad = HISTORY_ROBOT_DIAMETER_M / 2 + 0.1
    xs = [p[0] for p in norm]
    ys = [p[1] for p in norm]
    bounds = {
        "minX": _r(min(xs) - pad),
        "maxX": _r(max(xs) + pad),
        "minY": _r(min(ys) - pad),
        "maxY": _r(max(ys) + pad),
    }

    # Prefer the firmware's own duration, matching helpers.ts::sessionDuration.
    if summary and float(summary.get("duration", 0) or 0) > 0:
        duration = float(summary["duration"])
    else:
        duration = norm[-1][3]

    path: list[float] = []
    for x, y, t, ts in norm:
        path.extend((_r(x), _r(y), _r(t, 1), _r(ts, 1)))

    _LOGGER.debug(
        "Replay session %s: %d poses, %d coverage cells, %d recharges, %.0fs",
        name, len(norm), len(coverage) // 3, len(recharges), duration,
    )

    return {
        "name": name,
        "session": session,
        "summary": summary,
        "cellSize": HISTORY_CELL_SIZE_M,
        "bounds": bounds,
        "duration": duration,
        "path": path,
        "coverage": coverage,
        "recharges": recharges,
    }


def render_replay_map(data: dict[str, Any], recording: bool = False) -> bytes | None:
    """Render the recorded cleaning path and coverage as a static PNG.

    The interactive card draws this same payload on a canvas. This compact
    server renderer keeps the built-in HA ``Cleaning replay`` camera useful
    too, instead of incorrectly showing the sparse LIDAR wall layer.
    """
    bounds = data.get("bounds")
    path = data.get("path") or []
    if not isinstance(bounds, dict) or len(path) < 8:
        return None

    try:
        min_x, max_x = float(bounds["minX"]), float(bounds["maxX"])
        min_y, max_y = float(bounds["minY"]), float(bounds["maxY"])
    except (KeyError, TypeError, ValueError):
        return None
    span_x, span_y = max_x - min_x, max_y - min_y
    if span_x <= 0 or span_y <= 0:
        return None

    size = HISTORY_IMAGE_SIZE
    pad = HISTORY_PAD_PX
    scale = min((size - pad * 2) / span_x, (size - pad * 2) / span_y)
    offset_x = pad + (size - pad * 2 - span_x * scale) / 2
    offset_y = pad + (size - pad * 2 - span_y * scale) / 2

    def point(x: float, y: float) -> tuple[float, float]:
        return offset_x + (x - min_x) * scale, offset_y + (max_y - y) * scale

    def blend(color: tuple[int, int, int, int]) -> tuple[int, int, int]:
        """Flatten an RGBA overlay onto the fixed dark map background."""
        alpha = color[3] / 255
        return tuple(
            round(channel * alpha + background * (1 - alpha))
            for channel, background in zip(color[:3], HISTORY_BG_COLOR)
        )

    image = Image.new("RGB", (size, size), HISTORY_BG_COLOR)
    draw = ImageDraw.Draw(image)
    grid_color = blend(HISTORY_GRID_COLOR)
    coverage_color = blend(HISTORY_COVERAGE_COLOR)
    path_color = blend(HISTORY_PATH_COLOR)
    start_color = blend(HISTORY_START_COLOR)
    end_color = blend(HISTORY_END_COLOR)
    grid_x = math.floor(min_x / HISTORY_GRID_STEP_M) * HISTORY_GRID_STEP_M
    while grid_x <= max_x:
        x, _ = point(grid_x, min_y)
        draw.line((x, pad, x, size - pad), fill=grid_color, width=1)
        grid_x += HISTORY_GRID_STEP_M
    grid_y = math.floor(min_y / HISTORY_GRID_STEP_M) * HISTORY_GRID_STEP_M
    while grid_y <= max_y:
        _, y = point(min_x, grid_y)
        draw.line((pad, y, size - pad, y), fill=grid_color, width=1)
        grid_y += HISTORY_GRID_STEP_M

    cell_px = HISTORY_CELL_SIZE_M * scale
    coverage = data.get("coverage") or []
    for i in range(0, len(coverage) - 2, 3):
        x, y = point(
            float(coverage[i]) * HISTORY_CELL_SIZE_M,
            float(coverage[i + 1]) * HISTORY_CELL_SIZE_M,
        )
        draw.rectangle(
            (x - cell_px / 2, y - cell_px / 2, x + cell_px / 2, y + cell_px / 2),
            fill=coverage_color,
        )

    coords = [
        point(float(path[i]), float(path[i + 1]))
        for i in range(0, len(path) - 3, 4)
    ]
    if len(coords) > 1:
        draw.line(coords, fill=path_color, width=2, joint="curve")
    if coords:
        sx, sy = coords[0]
        draw.ellipse((sx - 5, sy - 5, sx + 5, sy + 5), fill=start_color)
        ex, ey = coords[-1]
        if recording:
            draw.ellipse((ex - 6, ey - 6, ex + 6, ey + 6), outline=start_color, width=3)
        else:
            draw.ellipse((ex - 5, ey - 5, ex + 5, ey + 5), fill=end_color)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _pair_recharges(
    raw_recharges: list[dict[str, float]],
    norm: list[tuple[float, float, float, float]],
    t_origin: float,
) -> list[dict[str, float]]:
    """Give each recharge marker a start/end window on the timeline.

    The firmware writes the marker as soon as docking begins but keeps
    emitting snapshots until collection pauses, so the real charge window is
    the long gap a few lines later. Each marker claims its nearest unused
    gap, preferring one that starts after it.
    """
    if not raw_recharges:
        return []

    gaps: list[tuple[float, float]] = []
    for i in range(1, len(norm)):
        if norm[i][3] - norm[i - 1][3] >= GAP_MIN_S:
            gaps.append((norm[i - 1][3], norm[i][3]))

    used: set[int] = set()
    out: list[dict[str, float]] = []
    for r in raw_recharges:
        marker_ts = max(0.0, r["prevTs"] - t_origin)
        best_idx = -1
        best_score = math.inf
        for i, (start, _end) in enumerate(gaps):
            if i in used:
                continue
            # A gap before the marker is penalised 4x -- the pause always
            # follows the marker, so an earlier gap is a much worse match.
            distance = start - marker_ts if start >= marker_ts else (marker_ts - start) * 4
            if distance < best_score:
                best_score = distance
                best_idx = i
        if best_idx >= 0:
            used.add(best_idx)
            start, end = gaps[best_idx]
            out.append({"x": _r(r["x"]), "y": _r(r["y"]), "ts": _r(start, 1), "endTs": _r(end, 1)})
            continue
        # No large gap found -- a single-interval sliver still marks the spot.
        nxt = next((p[3] for p in norm if p[3] > marker_ts), None)
        out.append(
            {
                "x": _r(r["x"]),
                "y": _r(r["y"]),
                "ts": _r(marker_ts, 1),
                "endTs": _r(nxt if nxt is not None else marker_ts + POSE_INTERVAL_S, 1),
            }
        )
    return out


def _apply_alignment(
    norm: list[tuple[float, float, float, float]], align: tuple[int, int, int]
) -> list[tuple[float, float, float, float]]:
    """Rotate and shift a run onto the accumulated map's frame.

    `align` is (quarter, dx, dy) exactly as align_to_reference() returned it:
    quarter turns counter-clockwise, then a shift in *cells*.
    """
    quarter, dx, dy = align
    quarter %= 4
    shift_x = dx * HISTORY_CELL_SIZE_M
    shift_y = dy * HISTORY_CELL_SIZE_M
    out = []
    for x, y, t, ts in norm:
        if quarter == 1:
            x, y = -y, x
        elif quarter == 2:
            x, y = -x, -y
        elif quarter == 3:
            x, y = y, -x
        out.append((x + shift_x, y + shift_y, (t + quarter * 90.0) % 360.0, ts))
    return out


def _coverage_cells(
    norm: list[tuple[float, float, float, float]],
    brush: list[Any] | None = None,
) -> list[float]:
    """Stamp the robot footprint at each pose, keeping the earliest touch ts.

    Returned flat as [cx, cy, ts, cx, cy, ts, ...] so the payload stays small.
    """
    cell = HISTORY_CELL_SIZE_M
    radius_cells = math.ceil(HISTORY_ROBOT_DIAMETER_M / 2 / cell)
    # Precompute the footprint disc once instead of re-testing dx^2+dy^2 per pose.
    disc = [
        (dx, dy)
        for dx in range(-radius_cells, radius_cells + 1)
        for dy in range(-radius_cells, radius_cells + 1)
        if dx * dx + dy * dy <= radius_cells * radius_cells
    ]

    first_ts: dict[tuple[int, int], float] = {}

    def stamp(x: float, y: float, ts: float) -> None:
        cx = round(x / cell)
        cy = round(y / cell)
        for dx, dy in disc:
            first_ts.setdefault((cx + dx, cy + dy), ts)

    # Walk the path, not just the poses on it.
    #
    # Stamping only where a pose was logged leaves a hole whenever the robot
    # covered more ground between two of them than its own footprint spans.
    # Measured over a real session: the poses are 9 cm apart at the median and
    # that is fine, but 42 of 1768 intervals exceed the 33 cm footprint and one
    # reaches 166 cm. Those are the beads of cleaned floor with bare gaps
    # between them -- the robot did pass, the record just did not say so.
    # Filling the segment costs a few extra stamps only where the gap is real.
    # Only stamp where the brush was actually turning.
    #
    # A pose says where the robot was, not that it cleaned there. Measured on
    # this robot: ~1400 rpm while following a boundary, and exactly 0 in
    # ST_F5_PickedUp and ST_F6_CleaningErrRecovery — both of which happen
    # mid-session. Painting those stretches as cleaned floor overstates what
    # the robot did. A missing value means an older session that never recorded
    # the brush, and those keep their old behaviour rather than losing their
    # coverage entirely.
    step = HISTORY_ROBOT_DIAMETER_M / 4
    prev: tuple[float, float, float] | None = None
    for i, (x, y, _t, ts) in enumerate(norm):
        b = brush[i] if brush is not None and i < len(brush) else None
        if b is not None and float(b) < BRUSH_RUNNING_RPM:
            # Not cleaning here: keep the path continuous for the next
            # segment, but lay nothing down.
            prev = (x, y, ts)
            continue
        if prev is not None:
            px, py, pts = prev
            gap = math.hypot(x - px, y - py)
            if gap > step:
                for k in range(1, int(gap / step) + 1):
                    f = k * step / gap
                    if f >= 1.0:
                        break
                    stamp(px + (x - px) * f, py + (y - py) * f, pts + (ts - pts) * f)
        stamp(x, y, ts)
        prev = (x, y, ts)

    flat: list[float] = []
    for (cx, cy), ts in first_ts.items():
        flat.extend((cx, cy, _r(ts, 1)))
    return flat
