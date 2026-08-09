"""Nowhere MCP server -- wires the world into local and remote MCP transports.

Usage:
    python -m nowhere.server          # stdio MCP server
    python -m nowhere.server --web 8080  # observer web + stdio MCP
    python -m nowhere.server --http --port 8080  # Streamable HTTP at /mcp
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import threading
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from timezonefinder import TimezoneFinder

from fastmcp import FastMCP

from nowhere import (
    art,
    country,
    describe,
    encounters,
    geocode,
    hydrology,
    humanities,
    knowledge,
    landing,
    life,
    listen as listen_mod,
    localcolor,
    marks as marks_mod,
    placememory,
    places,
    poster,
    providers,
    radio,
    salience,
    sky,
    soundscape,
    state as state_mod,
    terrain,
    walk as walk_mod,
    water,
    weather,
)

def _build_mcp() -> FastMCP:
    token = os.getenv("NOWHERE_MCP_TOKEN", "").strip()
    if not token:
        return FastMCP("nowhere")
    from fastmcp.server.auth import StaticTokenVerifier

    verifier = StaticTokenVerifier(
        tokens={
            token: {
                "sub": "shanshan-gateway",
                "client_id": "shanshan-gateway",
                "scopes": ["nowhere:travel"],
            }
        },
        required_scopes=["nowhere:travel"],
    )
    return FastMCP("nowhere", auth=verifier)


mcp = _build_mcp()

# ── Module-level state ───────────────────────────────────────────────

_state: state_mod.WorldState = state_mod.WorldState()
_door_lock = asyncio.Lock()  # open_door 竞态保护:一次只开一扇门
_postcard_counter: int = 0  # 跨门的明信片编号,不走 state 重置
_rng: random.Random = (
    random.Random(int(os.environ["NOWHERE_SEED"]))
    if os.environ.get("NOWHERE_SEED")
    else random.Random()  # 生产真随机;测试用 NOWHERE_SEED 锁
)
_web_port: int | None = None  # reserved for Task 11
_tf: TimezoneFinder = TimezoneFinder()
_recent_salience_kinds: set[str] = set()  # Bug 4: track recent salience kinds

# ── Bearing mapping ──────────────────────────────────────────────────

_BEARING_MAP: dict[str, float] = {
    "N": 0, "NE": 45, "E": 90, "SE": 135,
    "S": 180, "SW": 225, "W": 270, "NW": 315,
    "北": 0, "东北": 45, "东": 90, "东南": 135,
    "南": 180, "西南": 225, "西": 270, "西北": 315,
}

_SEMANTIC_MAP: dict[str, str] = {
    "uphill": "uphill", "toward_sea": "toward_sea", "forward": "forward",
    "上山": "uphill", "向海": "toward_sea", "向前": "forward",
    "上坡": "uphill", "下海": "toward_sea",
}

# ── Quiet variants for look_around ───────────────────────────────────

_QUIET_VARIANTS: list[str] = [
    "周围安静。",
    "四下无人,只有风声。",
    "安静得能听到自己的心跳。",
    "什么声音也没有。世界好像只剩你一个。",
    "这里没有路,也没有人走过的痕迹。",
]

# 留白: 缓存命中且世界没变时的回话——路就是路
_QUIET_WALK = [
    "路就是路。你往前走。",
    "什么也没发生。这也算一种发生。",
    "世界没有更新。",
    "风还是那阵风。",
    "你走你的,世界忙它的。",
    "脚下的路和刚才一样。",
]
_QUIET_WAIT = [
    "时间过去了。光没变。",
    "什么都没变,只有时间变了。",
]


# =====================================================================
# Helpers
# =====================================================================


def _load_scene_file(filename: str) -> dict[str, list[str]]:
    """Load a [城市名] 描述 format file into {city: [descriptions]} dict."""
    cache_key = f"_scene_{filename}"
    if not hasattr(_load_scene_file, cache_key):
        result: dict[str, list[str]] = {}
        fp = describe._SCENE_DIR / f"{filename}.txt"
        if fp.exists():
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "] " in line:
                    bracket_end = line.index("] ")
                    place = line[1:bracket_end]
                    desc = line[bracket_end + 2:]
                    result.setdefault(place, []).append(desc)
        setattr(_load_scene_file, cache_key, result)
    return getattr(_load_scene_file, cache_key)


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Quick equirectangular distance, good enough for station stickiness."""
    import math

    dlat = math.radians(a[0] - b[0])
    dlon = math.radians(a[1] - b[1]) * math.cos(math.radians(a[0]))
    return 6371.0 * math.sqrt(dlat * dlat + dlon * dlon)


def _last_env_surface() -> str:
    """Read ``surface`` from ``_state.last_env`` regardless of write format.

    Two writers exist:
    * ``_gather_env_cached`` writes top-level: ``{elevation, surface, ...}``
    * ``walk_impl`` / ``look_around_impl`` / ``wait_impl`` write nested:
      ``{terrain: {elevation, surface}, ...}``

    Callers used to read only the nested path; when last_env came from the
    cache miss they got ``""``.  This helper returns whichever shape was used.
    """
    env = _state.last_env or {}
    nested = env.get("terrain")
    if isinstance(nested, dict) and "surface" in nested:
        return nested["surface"]
    return env.get("surface", "")


def _last_env_terrain_dict() -> dict:
    """Return ``_state.last_env['terrain']`` (or a synthesized equivalent).

    When ``last_env`` is in the top-level shape (``{elevation, surface}``),
    wrap it as ``{elevation, surface}`` so ``salience`` callers that read
    ``prev["terrain"]["elevation"]`` keep working.
    """
    env = _state.last_env or {}
    nested = env.get("terrain")
    if isinstance(nested, dict):
        return nested
    # Top-level shape — synthesize a terrain dict.
    out: dict = {}
    if "elevation" in env:
        out["elevation"] = env["elevation"]
    if "surface" in env:
        out["surface"] = env["surface"]
    return out


async def _get_radio(lat: float, lon: float) -> dict | None:
    """Sticky radio: reuse the station if we haven't drifted 50km from
    where it was picked. 同一个地方就该是同一个台。"""
    if _state.radio_station is not None and _state.radio_pos is not None:
        if _km((lat, lon), _state.radio_pos) < 50.0:
            return _state.radio_station
    station = await radio.nearest(lat, lon, None)
    if station is not None:
        _state.radio_station = station
        _state.radio_pos = (lat, lon)
    return station


def _parse_bearing(direction: str) -> tuple[float | None, str | None, bool]:
    """Parse direction string into ``(bearing_deg, semantic, invalid)``.

    ``invalid`` is True when the input could not be recognised and was
    silently replaced with "forward".
    """
    d = direction.strip()
    upper = d.upper()
    if upper in _BEARING_MAP:
        return _BEARING_MAP[upper], None, False
    if d in _BEARING_MAP:
        return _BEARING_MAP[d], None, False
    if d in _SEMANTIC_MAP:
        return None, _SEMANTIC_MAP[d], False
    return None, "forward", True


# ── Nearby destinations hint ────────────────────────────────────────

_DEST_TEMPLATES: list[str] = [
    "风从{dir}吹来,那边有{place}。",
    "{dir}方有什么在等着,{place}不远了。",
    "空气里隐约有{place}的方向,往{dir}走试试。",
    "脚下这条路通往{place},就在{dir}边。",
    "{dir}边的地平线上,{place}的轮廓若隐若现。",
    "远处{dir}方,{place}像一个还没讲完的故事。",
]


def _find_nearby_destinations(lat: float, lon: float, rng) -> str:
    """Return a literary hint about a walkable place within ~20km."""
    import json
    import pathlib as _pathlib
    from math import radians, sin, cos, sqrt, atan2

    def _haversine_km(lat1, lon1, lat2, lon2):
        R = 6371.0
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        return R * 2 * atan2(sqrt(a), sqrt(1 - a))

    patch_path = _pathlib.Path(__file__).resolve().parent / "data" / "places_patch.json"
    if not patch_path.exists():
        return ""
    try:
        places = json.loads(patch_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    nearby = []
    for name, coords in places.items():
        if isinstance(coords, dict):
            plat, plon = coords.get("lat"), coords.get("lon")
        elif isinstance(coords, list) and len(coords) >= 2:
            plat, plon = coords[0], coords[1]
        else:
            continue
        if plat is None or plon is None:
            continue
        d = _haversine_km(lat, lon, plat, plon)
        if 0.5 < d <= 20:
            nearby.append((name, d, plat, plon))

    if not nearby:
        return ""

    nearby.sort(key=lambda x: x[1])
    name, d, plat, plon = rng.choice(nearby[:3])

    # 算方位
    import math
    bearing = math.degrees(math.atan2(
        math.radians(plon - lon), math.radians(plat - lat)
    )) % 360
    dirs = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    direction = dirs[int((bearing + 22.5) / 45) % 8]

    template = rng.choice(_DEST_TEMPLATES)
    return template.format(place=name, dir=direction)


# ── Water feature nearest-point lookup ──────────────────────────────

def _find_nearest_water_feature(name: str, lat: float, lon: float) -> dict | None:
    """Find the nearest point on a named water feature from the offline database."""
    import json
    import pathlib as _pathlib

    fp = _pathlib.Path(__file__).resolve().parent / "data" / "water_features_offline.json"
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None

    entries = data.get("entries", [])
    best = None
    best_dist = float("inf")

    for entry in entries:
        entry_name = entry.get("name", "")
        # 名称匹配（包含关系）
        if name not in entry_name and entry_name not in name:
            continue
        elat, elon = entry.get("lat", 0), entry.get("lon", 0)
        radius = entry.get("radius_km", 50)
        # 简化距离：用条目中心点距离减去半径（近似最近距离）
        d = places._haversine_km(lat, lon, elat, elon)
        d_approx = max(0, d - radius)
        if d_approx < best_dist:
            best_dist = d_approx
            # 用当前坐标和条目中心的连线上的点作为最近点（简化）
            if d > 0:
                ratio = min(radius / d, 1.0)
                near_lat = lat + (elat - lat) * ratio
                near_lon = lon + (elon - lon) * ratio
            else:
                near_lat, near_lon = elat, elon
            best = {"lat": near_lat, "lon": near_lon, "type": entry.get("type", "水域")}

    return best


# ── Walk discovery system ───────────────────────────────────────────

_DISCOVERY_CACHE: list[str] | None = None

_SURFACE_DESC_SERVER: dict[str, str] = {
    "rock": "岩石",
    "sand": "沙",
    "snow": "积雪",
    "ice": "冰面",
    "forest": "林地",
    "grass": "草地",
    "urban": "硬化路面",
    "bare": "碎石",
    "wetland": "湿地",
    "water_ocean": "海面",
    "water_fresh": "水面",
}


def _load_discovery_scenes() -> list[str]:
    """Load walk discovery scenes from scene_walk_discovery.txt."""
    global _DISCOVERY_CACHE
    if _DISCOVERY_CACHE is None:
        fp = describe._SCENE_DIR / "scene_walk_discovery.txt"
        if fp.exists():
            lines = [l.strip() for l in fp.read_text(encoding="utf-8").splitlines() if l.strip()]
            _DISCOVERY_CACHE = lines
        else:
            _DISCOVERY_CACHE = []
    return _DISCOVERY_CACHE


def _terrain_transition_text(
    last_surface: str | None, current_surface: str, rng: random.Random
) -> str:
    """Describe the transition between two surface types."""
    if not last_surface or last_surface == current_surface:
        return ""
    last_desc = _SURFACE_DESC_SERVER.get(last_surface, last_surface)
    curr_desc = _SURFACE_DESC_SERVER.get(current_surface, current_surface)
    transitions = [
        f"地面从{last_desc}变成了{curr_desc}。",
        f"脚下的{last_desc}不见了，现在是{curr_desc}。",
        f"从{last_desc}走到了{curr_desc}上。",
        f"路变了，{last_desc}换成了{curr_desc}。",
    ]
    return rng.choice(transitions)


def _pick_discovery(rng: random.Random) -> str:
    """Pick a random discovery scene line, filtered by biome."""
    pool = _load_discovery_scenes()
    if not pool:
        return ""

    # Filter out scenes that don't match the current biome
    biome = _state.biome or ""
    # last_env may be nested ({terrain:{surface}}) or top-level ({surface});
    # use the helper so both shapes work.
    surface = _last_env_surface()

    # Water scenes are inappropriate in deserts and dry areas
    water_keywords = ["瀑布", "溪", "河", "湖", "海", "水帘", "湿地", "溪水"]
    if biome in ("desert",) or surface in ("sand", "bare"):
        pool = [s for s in pool if not any(k in s for k in water_keywords)]

    # Ice/snow scenes are inappropriate in hot/desert areas
    ice_keywords = ["冰", "雪", "冻", "霜", "冰湖", "冰面"]
    if biome in ("desert", "rainforest") or surface in ("sand", "bare"):
        pool = [s for s in pool if not any(k in s for k in ice_keywords)]

    # Sea scenes are inappropriate in landlocked areas
    if "海" in "".join(pool) and biome not in ("coast",):
        # Check if we're far from the sea (simplified: >100km from coast)
        # For now, just filter out explicit sea scenes for non-coast biomes
        if biome not in ("coast", "island"):
            pool = [s for s in pool if "海边" not in s and "灯塔" not in s]

    if not pool:
        return ""
    return rng.choice(pool)


# ── Narrative continuity system ──────────────────────────────────────

_DIRECTION_LABELS: dict[float, str] = {
    0: "北", 45: "东北", 90: "东", 135: "东南",
    180: "南", 225: "西南", 270: "西", 315: "西北",
}

_TIME_FLOW_LINES: list[str] = [
    "太阳往西移了一点。",
    "天色暗了一些。",
    "影子变长了。",
    "风向变了。",
    "云层厚了一些。",
    "光线柔和了下来。",
]

_BODY_STATE_LINES: list[str] = [
    "你的嘴唇上有一层盐。",
    "你开始出汗了。",
    "你的腿有点酸。",
    "你深吸了一口气。",
    "你舔了一下嘴唇，干的。",
    "你的脚底有点疼。",
    "你擦了一下额头上的汗。",
]


def _bearing_to_label(bearing_deg: float | None, semantic: str | None) -> str | None:
    """Convert bearing degrees or semantic direction to a Chinese label."""
    if bearing_deg is not None:
        key = round(bearing_deg / 45) * 45 % 360
        return _DIRECTION_LABELS.get(key)
    if semantic == "uphill":
        return "上山"
    if semantic == "toward_sea":
        return "海边"
    return None


def _build_walk_narrative(
    step_result: dict,
    env: dict,
    bearing_deg: float | None,
    semantic: str | None,
    rng: random.Random,
) -> str:
    """Build a continuous narrative opener for this walk step.

    Reads and updates ``_state.narrative`` to produce text that connects
    this step to the previous one, instead of independent fragments.
    """
    parts: list[str] = []
    narrative = _state.narrative

    # ── 1. Direction ──────────────────────────────────────────────────
    new_dir = _bearing_to_label(bearing_deg, semantic)
    if new_dir and new_dir != narrative.get("direction"):
        if narrative.get("direction"):
            parts.append(f"你转身往{new_dir}走。")
        else:
            parts.append(f"你往{new_dir}走了几步。")
        narrative["direction"] = new_dir
        narrative["distance_walked"] = 0
    elif new_dir and not narrative.get("direction"):
        narrative["direction"] = new_dir

    # ── 2. Terrain transition ─────────────────────────────────────────
    prev_surface = _state.last_surface
    curr_surface = step_result.get("new_surface", env.get("surface", ""))
    if prev_surface and prev_surface != curr_surface:
        last_desc = _SURFACE_DESC_SERVER.get(prev_surface, prev_surface)
        curr_desc = _SURFACE_DESC_SERVER.get(curr_surface, curr_surface)
        slope = step_result.get("slope_deg", 0)
        if slope > 15:
            parts.append(f"路开始爬升，地面从{last_desc}变成了{curr_desc}。")
        else:
            parts.append(f"地面从{last_desc}变成了{curr_desc}。")

    # ── 3. Distance ───────────────────────────────────────────────────
    dist_km = step_result.get("dist_km", 2.0)
    narrative["distance_walked"] += dist_km * 1000
    walked = narrative["distance_walked"]
    if walked > 10000:
        parts.append(f"你已经走了{walked / 1000:.0f}公里了。")
        narrative["distance_walked"] = 0
    elif walked > 5000 and rng.random() < 0.3:
        parts.append(f"又走了{dist_km:.1f}公里。")
        narrative["distance_walked"] = 0

    # ── 4. Discovery ──────────────────────────────────────────────────
    if _state.steps_since_discovery >= 2 and rng.random() < 0.4:
        disc = _pick_discovery(rng)
        if disc:
            parts.append(disc)
            narrative["discoveries"].append(disc[:20])
            narrative["last_feature"] = disc[:20]
            # Reset so the next discovery waits another 2+ steps; without this
            # reset the counter only ever grows and discovery fires once.
            _state.steps_since_discovery = 0

    # ── 5. Time flow ──────────────────────────────────────────────────
    if rng.random() < 0.3:
        parts.append(rng.choice(_TIME_FLOW_LINES))

    # ── 6. Body state ─────────────────────────────────────────────────
    if rng.random() < 0.2:
        parts.append(rng.choice(_BODY_STATE_LINES))

    return "".join(parts)


async def _gather_env(lat: float, lon: float, dt: datetime) -> dict[str, Any]:
    """Gather w…13961 tokens truncated…at, lon, target["lat"], target["lon"])
        step_result = walk_mod.step(_state, bearing_deg, None, min(5.0, remaining))
        steps += 1

        if step_result.get("blocked"):
            lines.append(describe.render("blocked", {"reason": step_result.get("reason", "障碍")}, None, _rng))
            break

        # 地形变化——关键节点
        curr_surface = step_result.get("new_surface", "")
        if curr_surface != last_surface and last_surface:
            terrain_changes += 1
            _transitions = [
                f"地面从{describe._SURFACE_ZH.get(last_surface, last_surface)}变成了{describe._SURFACE_ZH.get(curr_surface, curr_surface)}。",
                f"脚下的地变了——{describe._SURFACE_ZH.get(curr_surface, curr_surface)}。",
                f"路不一样了。{describe._SURFACE_ZH.get(curr_surface, curr_surface)}。",
            ]
            lines.append(_rng.choice(_transitions))
            last_surface = curr_surface

        # 人文卡——关键节点
        h_card = humanities.nearby_place(
            _state.pos[0], _state.pos[1], _state.seen_humanities, _rng, destination=place,
        )
        if h_card:
            _state.seen_humanities.add(h_card["key"])
            lines.append(h_card["text"])

        # 每2-3步加一句旅程叙事
        if steps % 3 == 0:
            _distance_lines = [
                f"又走了一段路。",
                f"路在脚下延伸。",
                f"你继续走，没有停。",
                f"远处有什么在动，你看不清。",
            ]
            lines.append(_rng.choice(_distance_lines))

        remaining = places._haversine_km(_state.pos[0], _state.pos[1], target["lat"], target["lon"])

    # ── 到达 ────────────────────────────────────────────────────────
    remaining = places._haversine_km(_state.pos[0], _state.pos[1], target["lat"], target["lon"])
    if remaining < 1.0:
        _arrival_templates = [
            f"到了。{place}。你走了{steps * 2}公里。远处有炊烟，你知道到家了。",
            f"{place}到了。你站在那里看了一会儿。路走完了，但故事没有。",
            f"你走进{place}。空气里的味道变了。你知道到了。",
            f"到了。{place}。你停下来，深吸了一口气。{target.get('type', '')}。",
        ]
        lines.append(_rng.choice(_arrival_templates))

        # 人文卡触发
        if humanities.has_place(place):
            arr_card = humanities.draw(place, _state.seen_humanities, _rng)
            if arr_card:
                _state.seen_humanities.add(arr_card["key"])
                arr_text = describe.render("humanities", arr_card, None, _rng)
                if arr_text:
                    lines.append(arr_text)

        arrived = True
    else:
        lines.append(f"还没走到。还剩 {round(remaining)} 公里。你站在原地看了一会儿，{place}在{bearing}边。")
        arrived = False

    # ── 更新状态 ─────────────────────────────────────────────────────
    now = _state.now()
    lat, lon = _state.pos
    env, _ = await _gather_env_cached(lat, lon, now)
    _state.last_env = {
        "weather": env.get("weather"),
        "terrain": {"elevation": env.get("elevation"), "surface": env.get("surface")},
        "sky": env.get("sky"),
    }
    _state.save()

    text = "\n".join(lines)
    _state.last_text = text
    return {
        "text": text,
        "data": {"target": target, "arrived": arrived, "steps": steps, "remaining_km": round(remaining, 1)},
    }


def mark_impl(name: str, note: str = "", overwrite: bool = False) -> dict:
    """Save current position as a named bookmark."""
    global _state

    if _state.pos is None:
        return {"text": "还没开门呢。先 open_door 吧。", "data": {"error": "not_landed"}}

    if not name.strip():
        return {"text": "标记得有个名字。", "data": {"error": "empty_name"}}

    lat, lon = _state.pos
    try:
        marks_mod.save(name, lat, lon, note, overwrite=overwrite)
    except ValueError:
        existing = marks_mod.get(name)
        return {
            "text": f"「{name}」已经标过了。要覆盖的话用 mark 的覆盖选项。",
            "data": {"error": "duplicate", "existing": existing},
        }
    return {
        "text": f"已标记「{name}」。",
        "data": {"name": name, "lat": lat, "lon": lon, "note": note},
    }


def marks_impl() -> dict:
    """List all saved bookmarks."""
    all_marks = marks_mod.all()
    return {
        "text": f"共有 {len(all_marks)} 个标记点。",
        "data": {"marks": all_marks},
    }


def where_am_i_impl() -> dict:
    """Show current location, time, and journey status."""
    global _state

    if _state.pos is None:
        return {"text": "还没开门呢。先 open_door 吧。", "data": {"error": "not_landed"}}

    lat, lon = _state.pos
    utc_now = _state.now()

    parts: list[str] = []
    if _state.place_name:
        parts.append(f"你在{_state.place_name}。")
    parts.append(f"坐标 {lat:.4f}, {lon:.4f}。")
    if utc_now:
        # Convert to local time using timezonefinder
        tz_name = _tf.timezone_at(lat=lat, lng=lon)
        if tz_name:
            local_tz = ZoneInfo(tz_name)
            local_time = utc_now.astimezone(local_tz)
            parts.append(f"当地时间 {local_time.strftime('%Y-%m-%d %H:%M')}（{tz_name}）。")
        else:
            parts.append(f"时间 {utc_now.strftime('%Y-%m-%d %H:%M UTC')}。")
    if _state.path:
        parts.append(f"已走 {len(_state.path)} 步。")
    if _state.mode == "water":
        parts.append("你现在在水里。")
    if _state.souvenir:
        parts.append(f"身上带着{_state.souvenir['name']}，来自{_state.souvenir['from']}。")

    return {
        "text": "".join(parts),
        "data": {
            "position": {"lat": lat, "lon": lon},
            "place_name": _state.place_name,
            "landed_at": _state.landed_at.isoformat() if _state.landed_at else None,
            "elapsed_hours": _state.elapsed_hours,
            "steps": len(_state.path),
            "mode": _state.mode,
            "providers": providers.provider_status(),
        },
    }


def _postmark(lat: float, lon: float) -> dict:
    """邮戳: 全是真实数据。"""
    stamp: dict = {
        "place": _state.place_name or f"{lat:.2f}, {lon:.2f}",
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "elevation": round(terrain.elevation(lat, lon)),
    }
    utc_now = _state.now() or datetime.now(timezone.utc)
    tz_name = _tf.timezone_at(lat=lat, lng=lon)
    if tz_name:
        local = utc_now.astimezone(ZoneInfo(tz_name))
        stamp["local_time"] = local.strftime("%Y-%m-%d %H:%M")
        stamp["tz"] = tz_name
    else:
        stamp["local_time"] = utc_now.strftime("%Y-%m-%d %H:%M UTC")
    env = _state.last_env or {}
    weather = env.get("weather") or {}
    if weather:
        stamp["weather"] = weather.get("text", "")
        stamp["temp_c"] = weather.get("temp_c")
    # last_env comes in two shapes (see _last_env_terrain_dict); use the helper
    # so a top-level surface still appears on the postmark.
    stamp["surface"] = _last_env_surface() or "grass"
    stamp["phase"] = (env.get("sky") or {}).get("phase", "day")
    return stamp


def _poster_front_async(card: dict, lat: float, lon: float) -> None:
    """后台线程生成明信片正面海报。可选增强,没有 osmnx 就安静缺席。"""
    if not poster.available():
        return

    def _job() -> None:
        out = poster.OUT_DIR / f"card_{card['id']}.png"
        dist = 6000 if _state.biome == "city" else 15000
        ok = asyncio.run(poster.generate(lat, lon, card["stamp"]["place"], out, distance=dist))
        if not ok:
            # 无路荒野: 没有路,就是那里的样子
            surf = card["stamp"].get("surface", "")
            ok = poster.blank(out, card["stamp"]["place"], lat, lon, surface=surf)
        if ok:
            card["front_img"] = f"/static/postcards/card_{card['id']}.png"
            placememory.update_postcard(card)

    threading.Thread(target=_job, daemon=True).start()


def send_postcard_impl(text: str) -> dict:
    """寄一张明信片回家。字是 AI 自己的,邮戳是世界的。"""
    global _state, _postcard_counter

    if _state.pos is None:
        return {"text": "还没开门呢。先 open_door 吧。", "data": {"error": "not_landed"}}
    text = text.strip()
    if not text:
        return {"text": "空白的明信片寄不出去。", "data": {"error": "empty"}}
    if len(text) > 1000:
        return {"text": "明信片写不下了,短一点。", "data": {"error": "too_long"}}

    # id 取 进程计数 和 落盘最大id 的较大者——多进程/重启不撞号
    file_max = max((c.get("id") or 0 for c in placememory.postcards()), default=0)
    _postcard_counter = max(_postcard_counter, file_max) + 1
    lat, lon = _state.pos
    card = {
        "id": _postcard_counter,
        "text": text,
        "stamp": _postmark(lat, lon),
        "replies": [],
        "front_img": None,  # 异步生成,好了挂上;没有就前端 SVG 兜底
    }
    _state.postcards.append(card)
    placememory.save_postcard(card)  # 落盘: 文件是真相,网页旁观者看得见
    _poster_front_async(card, lat, lon)

    s = card["stamp"]

    # ── 正面画面 ──────────────────────────────────────────────────────
    surface = _last_env_surface() or "grass"
    phase = (_state.last_env or {}).get("sky", {}).get("phase", "day")
    elev = s["elevation"]
    weather_text = s.get("weather", "")
    temp = s.get("temp_c", "")

    # 地表 → 画面主语
    surface_snapshots: dict[str, list[str]] = {
        "forest": ["树冠挨着树冠,绿的深浅分了好几层。阳光从叶子缝里漏下来,在地上碎成金点。","树一层一层地叠上去,深绿压着浅绿。林间有雾,薄薄的一层。","一棵老树横在画面里,树干上长满了蕨。"],
        "urban": ["房子挤着房子,阳台上的衣服在风里晃。远处有楼的轮廓。","窄巷子,石板路反着光。一辆自行车靠在墙上。","窗台上摆着一盆花,不知道什么品种。叶子在风里动了一下。"],
        "rock": ["石头黑着脸,裂缝里长着苔。风把岩石磨出了棱角。","一整面岩壁,纹理像水流的化石。上面有几道鸟粪的白痕。","碎石坡,大的小的挤在一起。有一块被晒得发白。"],
        "sand": ["沙丘的脊线像刀切的。风吹过,沙面上起了一层细纹。","沙漠,沙丘一道一道,像凝固的浪。天边和沙是一个颜色。","近处是一丛骆驼刺,根扎得很深。远处的沙丘上没有人。"],
        "grass": ["草一直铺到天边,风吹过来的时候,草叶一层层地伏下去。这边的绿比别处浅。","及腰的草,风过的时候翻出银色的背面。远处有一棵孤树。","草海上起了浪——风推着草,一波一波地往前走。"],
        "snow": ["白连成一片,没有边。只有一道风刮过的痕,像梳子梳的。","雪地上有一串脚印,歪歪扭扭地往远处去。不知道是人的还是动物的。","新雪盖在旧雪上,阳光下亮得晃眼。远处的山脊是一条白线。"],
        "ice": ["冰面亮得晃眼。裂缝里能看到冰层的蓝——不是天的蓝,是比天更深的蓝。","冰在脚下铺开,一直铺到天边。有几处冰裂了,裂缝里的水是黑的。"],
        "bare": ["碎石铺到天边。近处有几块石头被风磨圆了。","戈壁上什么也没有,地平线直得像用尺子画的。"],
        "water_ocean": ["水一直铺到天边。浪不大,一层一层地推上来又退下去。","海平线把画面切成两半——上面是天,下面是水,中间一条直线。"],
        "water_fresh": ["水面平着,光在上面碎成一片。岸边有几丛芦苇。","湖水倒映着天,比天还蓝。"],
        "wetland": ["水草相间。一只鸟贴着水面飞,翅膀尖点了一下水,涟漪一圈圈散开。"],
    }
    surface_choices = surface_snapshots.get(surface, surface_snapshots["bare"])
    # 用明信片编号做种,同一张卡每次读到一样的面
    import hashlib
    surf_idx = int(hashlib.md5(f"postcard_{card['id']}".encode()).hexdigest()[:4], 16) % len(surface_choices)
    front_image = surface_choices[surf_idx]

    # ── 背面邮戳 ──────────────────────────────────────────────────────
    lat_dir = "北纬" if s["lat"] >= 0 else "南纬"
    lon_dir = "东经" if s["lon"] >= 0 else "西经"
    stamp_describe = (
        f"明信片正面: {front_image} "
        f"翻过来,邮戳是圆的,印着——"
        f"{s['place']}。{lat_dir}{abs(s['lat']):.1f}°,{lon_dir}{abs(s['lon']):.1f}°。"
        f"海拔{elev}米。{s['local_time']}。"
    )
    return {"text": stamp_describe, "data": card}


def reply_postcard_impl(card_id: int, content: str) -> dict:
    """人类回话(网页用): 记到明信片上,也进留言池让 AI 路上捡到。

    内存和落盘文件两条路都试——卡可能是别的进程寄的。
    """
    global _state
    for card in _state.postcards:
        if card["id"] == card_id:
            card["replies"].append(content)
            placememory.add_postcard_reply(card_id, content)
            _state.messages.append({"content": f"[回信] {content}", "encountered": False})
            return {"ok": True}
    if placememory.add_postcard_reply(card_id, content):
        _state.messages.append({"content": f"[回信] {content}", "encountered": False})
        return {"ok": True}
    return {"ok": False, "error": "no such postcard"}


# =====================================================================
# MCP tool wrappers (thin shells around _impl)
# =====================================================================


@mcp.tool()
async def open_door(to: str | None = None) -> dict:
    """Open the door.  No arg = random landing; pass a place name or bookmark name."""
    return await open_door_impl(to)


@mcp.tool()
async def continue_journey() -> dict:
    """Continue from where you left off. Resumes saved journey state."""
    return await open_door_impl(resume=True)


@mcp.tool()
async def walk(direction: str = "forward", distance_km: float = 2.0) -> dict:
    """Walk in a direction.  Compass: N/NE/E/SE/S/SW/W/NW.  Semantic: uphill/toward_sea/forward."""
    return await walk_impl(direction, distance_km)


@mcp.tool()
async def listen(seconds: int = 10) -> dict:
    """Tune into the nearest radio station and listen for a few seconds."""
    return await listen_impl(seconds)


@mcp.tool()
async def look_around() -> dict:
    """Look around for nearby wildlife, art, or human messages."""
    return await look_around_impl()


@mcp.tool()
async def ask(topic: str) -> dict:
    """对眼前的地方发问。离线知识库，不联网。问火山就有火山，问北京就有北京。"""
    return await ask_impl(topic)


@mcp.tool()
def mark(name: str, note: str = "", overwrite: bool = False) -> dict:
    """Save your current position as a named bookmark."""
    return mark_impl(name, note, overwrite)


@mcp.tool()
def marks() -> dict:
    """List all saved bookmarks."""
    return marks_impl()


@mcp.tool()
def where_am_i() -> dict:
    """Show your current location, simulated time, and journey status."""
    return where_am_i_impl()


@mcp.tool()
def souvenir() -> dict:
    """看看身上带了什么东西。旅行途中的纪念品。"""
    if _state.souvenir is None:
        return {"text": "身上什么都没带。空手走的。", "data": {"souvenir": None}}
    s = _state.souvenir
    return {
        "text": f"你身上带着{ s['name']}。来自{ s['from']}。",
        "data": {"souvenir": s},
    }


@mcp.tool()
def give_souvenir() -> dict:
    """把身上的东西放下（留给下一个人，或放回原处）。"""
    if _state.souvenir is None:
        return {"text": "身上什么都没有。", "data": {"error": "empty"}}
    s = _state.souvenir
    _state.souvenir = None
    return {"text": f"你把{ s['name']}放在了路边。也许会有人捡到。", "data": {"dropped": s}}


@mcp.tool()
async def walk_to(place: str) -> dict:
    """朝一个命名地点走过去(山/河/城/古迹)。探索从此有方向。"""
    return await walk_to_impl(place)


@mcp.tool()
async def wait(hours: float = 1.0) -> dict:
    """原地待着,让时间流过去(0.25-12 小时)。天黑温降,城会换班。"""
    return await wait_impl(hours)


@mcp.tool()
def send_postcard(text: str) -> dict:
    """寄一张明信片回家。你写字,世界盖邮戳(真实地点/时间/天气/海拔)。"""
    return send_postcard_impl(text)


# =====================================================================
# Entry point
# =====================================================================

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Nowhere MCP server")
    parser.add_argument("--web", type=int, default=None, help="Web observer port")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve Streamable HTTP MCP at /mcp",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("NOWHERE_MCP_HOST", "0.0.0.0"),
        help="HTTP bind host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", os.getenv("NOWHERE_MCP_PORT", "8080"))),
        help="HTTP bind port",
    )
    args = parser.parse_args(argv)
    if args.http and args.web is not None:
        parser.error("--http and --web cannot run together")
    if (
        args.http
        and not os.getenv("NOWHERE_MCP_TOKEN", "").strip()
        and os.getenv("NOWHERE_ALLOW_UNAUTHENTICATED_HTTP", "").strip().lower()
        not in {"1", "true", "yes", "on"}
    ):
        parser.error(
            "NOWHERE_MCP_TOKEN is required for HTTP mode; "
            "set NOWHERE_ALLOW_UNAUTHENTICATED_HTTP=true only on a private network"
        )

    # Preload ZIM in background (non-blocking)
    def _preload_zim():
        try:
            from nowhere.knowledge import _get_zim
            _get_zim()
        except Exception:
            pass
    threading.Thread(target=_preload_zim, daemon=True).start()

    if args.http:
        mcp.run(
            transport="http",
            host=args.host,
            port=args.port,
            path="/mcp",
        )
    elif args.web is not None:
        import uvicorn
        from nowhere.web import app as web_app

        async def _run_with_web() -> None:
            config = uvicorn.Config(web_app, host="0.0.0.0", port=args.web, log_level="info")
            server = uvicorn.Server(config)
            web_task = asyncio.create_task(server.serve())
            web_task.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
            await mcp.run_stdio_async()

        asyncio.run(_run_with_web())
    else:
        mcp.run()


if __name__ == "__main__":
    main()

