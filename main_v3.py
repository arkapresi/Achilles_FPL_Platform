from pathlib import Path
import json
import time
from typing import Dict, Any
import asyncio

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from european_competitions import (
    load_groups, build_schedule, get_histories, build_group_tables,
    qualification_rows,
)

ROOT = Path(__file__).parent
SETTINGS = ROOT / "data" / "settings.json"
LMS_CACHE_FILE = ROOT / "data" / "lms_cache.json"

app = FastAPI(title="Achilles FPL Platform")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

_cache: Dict[str, Any] = {}
# Limit concurrent calls to the FPL API. The LMS page can otherwise open
# dozens of connections at once and intermittently hit ConnectTimeout.
_fpl_semaphore = asyncio.Semaphore(8)


def load_settings():
    return json.loads(SETTINGS.read_text())


def save_settings(s):
    SETTINGS.write_text(json.dumps(s, indent=2))


def load_lms_cache():
    """Load the last successful LMS result so a temporary FPL API failure
    cannot make the LMS page appear blank after a browser refresh."""
    try:
        if LMS_CACHE_FILE.exists():
            return json.loads(LMS_CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    return None


def save_lms_cache(data):
    """Persist the latest usable LMS result."""
    try:
        LMS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LMS_CACHE_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


async def fpl_get(path, ttl=30):
    """Fetch an FPL API endpoint reliably.

    The FPL API can intermittently timeout or reject bursts of requests.
    Retry transient failures, limit concurrent connections, and fall back to
    a stale cached response when one exists so a temporary API problem does
    not blank the application page.
    """
    now = time.time()
    key = path

    if key in _cache and now - _cache[key][0] < ttl:
        return _cache[key][1]

    url = (
        load_settings()["base_url"].rstrip("/")
        + "/"
        + path.lstrip("/")
    )

    last_error = None

    for attempt in range(4):
        try:
            async with _fpl_semaphore:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=12.0,
                        read=40.0,
                        write=15.0,
                        pool=15.0,
                    ),
                    headers={"User-Agent": "Achilles-FPL-Platform"},
                ) as c:
                    r = await c.get(url)
                    # Retry temporary server/rate-limit responses.
                    if r.status_code in {429, 500, 502, 503, 504}:
                        r.raise_for_status()
                    r.raise_for_status()
                    data = r.json()

            _cache[key] = (time.time(), data)
            return data

        except httpx.HTTPStatusError as exc:
            last_error = exc
            status = exc.response.status_code
            if status not in {429, 500, 502, 503, 504}:
                raise

        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc

        if attempt < 3:
            await asyncio.sleep(1.5 * (attempt + 1))

    # If the API is temporarily unreachable but this endpoint was previously
    # fetched, use the stale value rather than taking down the page.
    if key in _cache:
        return _cache[key][1]

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Unable to retrieve FPL API endpoint: {path}")


async def get_bootstrap():
    return await fpl_get("bootstrap-static/", 60)


def build_player_map(bootstrap):
    return {
        p.get("id"): p
        for p in bootstrap.get("elements", [])
    }


def player_display_position(player):
    element_type = player.get("element_type")

    return {
        1: "GK",
        2: "DEF",
        3: "MID",
        4: "FWD",
    }.get(element_type, "—")


def point_breakdown(player, stats):
    """
    Return a simple list of player-performance statistics for the
    Manager Profile. GW-specific values come from event/{gw}/live/.
    """
    return [
        ("Minutes", stats.get("minutes", 0), None),
        ("Goals", stats.get("goals_scored", 0), None),
        ("Assists", stats.get("assists", 0), None),
        ("Clean sheets", stats.get("clean_sheets", 0), None),
        ("Goals conceded", stats.get("goals_conceded", 0), None),
        ("Saves", stats.get("saves", 0), None),
        ("Bonus", stats.get("bonus", 0), None),
        ("Yellow cards", stats.get("yellow_cards", 0), None),
        ("Red cards", stats.get("red_cards", 0), None),
        ("Own goals", stats.get("own_goals", 0), None),
        ("Penalty saves", stats.get("penalties_saved", 0), None),
        ("Penalty misses", stats.get("penalties_missed", 0), None),
        ("BPS", stats.get("bps", 0), None),
        ("FPL points", stats.get("total_points", 0), None),
    ]


async def get_league_data(s):
    standings_data = await fpl_get(
        f"leagues-classic/{s['league_id']}/standings/?page_standings=1",
        30,
    )

    standings = standings_data.get("standings", {}).get("results", [])
    league_name = standings_data.get("league", {}).get(
        "name",
        s["league_name"],
    )

    # Confirm current Gameweek from official FPL bootstrap data.
    bootstrap = await get_bootstrap()
    events = bootstrap.get("events", [])

    current_event = next(
        (e for e in events if e.get("is_current")),
        None,
    )

    if current_event is None:
        current_event = next(
            (e for e in events if e.get("is_next")),
            None,
        )

    current_gw = current_event.get("id") if current_event else None
    current_gw_name = f"GW{current_gw}" if current_gw else "—"

    for row in standings:
        row["rank_movement"] = (
            (row.get("last_rank") or row.get("rank", 0))
            - row.get("rank", 0)
        )

    gw_scores = [r.get("event_total", 0) for r in standings]
    highest = max(gw_scores) if gw_scores else None
    lowest = min(gw_scores) if gw_scores else None

    gw_winner = next(
        (r for r in standings if r.get("event_total") == highest),
        None,
    )

    lowest_manager = next(
        (r for r in standings if r.get("event_total") == lowest),
        None,
    )

    return {
        "standings": standings,
        "league_name": league_name,
        "current_gw": current_gw,
        "current_gw_name": current_gw_name,
        "gw_winner": gw_winner,
        "highest": highest,
        "lowest": lowest,
        "lowest_manager": lowest_manager,
    }


async def get_manager_profile(entry_id: int, event: int, s, league_row=None, current_gw=None):
    bootstrap = await get_bootstrap()
    player_map = build_player_map(bootstrap)

    # Manager history/profile. For the live/current GW we bypass our local
    # cache because the FPL history endpoint can lag while scores are being
    # processed.
    history = await fpl_get(
        f"entry/{entry_id}/history/",
        0 if current_gw == event else 30,
    )

    manager_info = await fpl_get(
        f"entry/{entry_id}/",
        0 if current_gw == event else 30,
    )

    # GW picks contain the manager's actual squad, captain and multipliers.
    picks_data = await fpl_get(
        f"entry/{entry_id}/event/{event}/picks/",
        0 if current_gw == event else 30,
    )
    picks = picks_data.get("picks", [])

    # Player-level GW statistics come from the official live endpoint.
    live_data = await fpl_get(
        f"event/{event}/live/",
        0 if current_gw == event else 30,
    )

    live_elements = {
        row.get("id"): row.get("stats", {})
        for row in live_data.get("elements", [])
    }

    for pick in picks:
        element_id = pick.get("element")
        player = player_map.get(element_id, {})

        # Preserve the original FPL squad position (1-15) before replacing
        # pick["position"] with the display position.
        pick["squad_position"] = pick.get("position")
        pick["player"] = player
        pick["position"] = player_display_position(player)
        pick["display_name"] = (
            f"{player.get('first_name', '')} "
            f"{player.get('second_name', '')}"
        ).strip()

        gw_stats = live_elements.get(element_id, {})

        pick["stats"] = {
            "minutes": gw_stats.get("minutes", 0),
            "goals_scored": gw_stats.get("goals_scored", 0),
            "assists": gw_stats.get("assists", 0),
            "clean_sheets": gw_stats.get("clean_sheets", 0),
            "goals_conceded": gw_stats.get("goals_conceded", 0),
            "saves": gw_stats.get("saves", 0),
            "bonus": gw_stats.get("bonus", 0),
            "yellow_cards": gw_stats.get("yellow_cards", 0),
            "red_cards": gw_stats.get("red_cards", 0),
            "own_goals": gw_stats.get("own_goals", 0),
            "penalties_saved": gw_stats.get("penalties_saved", 0),
            "penalties_missed": gw_stats.get("penalties_missed", 0),
            "bps": gw_stats.get("bps", 0),
            # event/{GW}/live/ is authoritative for player points. The picks
            # score is retained as a fallback if live data is incomplete.
            "total_points": gw_stats.get(
                "total_points",
                pick.get("points", 0),
            ),
        }

        # Expose BOTH names because different manager.html revisions used
        # either p.gw_points or p.points.
        pick["gw_points"] = pick["stats"]["total_points"]
        pick["points"] = pick["gw_points"]
        pick["effective_points"] = (
            pick["gw_points"] * pick.get("multiplier", 1)
        )

        # IMPORTANT: manager.html uses boolean `captain` / `vice_captain`
        # fields. Keep these as booleans so a vice-captain is never mistaken
        # for a captain. The FPL picks endpoint provides the authoritative
        # is_captain / is_vice_captain flags; multiplier is only a fallback
        # for captaincy identification.
        is_captain = bool(pick.get("is_captain"))
        is_vice_captain = bool(pick.get("is_vice_captain"))
        multiplier = int(pick.get("multiplier", 0) or 0)

        pick["is_captain"] = bool(is_captain or multiplier >= 3)
        pick["is_vice_captain"] = bool(is_vice_captain)
        pick["captain"] = pick["is_captain"]
        pick["vice_captain"] = pick["is_vice_captain"]

        if pick["is_captain"]:
            pick["captain_label"] = (
                "Triple Captain" if multiplier >= 3 else "Captain"
            )
            pick["role"] = pick["captain_label"]
        elif pick["is_vice_captain"]:
            pick["captain_label"] = "Vice-Captain"
            pick["role"] = "Vice-Captain"
        elif multiplier == 1:
            pick["captain_label"] = ""
            pick["role"] = "Starting XI"
        else:
            pick["captain_label"] = ""
            pick["role"] = "Bench"

        pick["breakdown"] = point_breakdown(player, pick["stats"])

    # FPL positions 1-11 are the starting XI; 12-15 are bench.
    starting = [
        p for p in picks
        if 1 <= p.get("squad_position", 99) <= 11
    ]
    bench = [
        p for p in picks
        if 12 <= p.get("squad_position", 99) <= 15
    ]

    if not starting and picks:
        starting = [p for p in picks if p.get("multiplier", 0) > 0]
        bench = [p for p in picks if p.get("multiplier", 0) == 0]

    history_rows = history.get("current", [])

    # The manager history endpoint can lag behind the live classic-league
    # standings during the current GW.  The Manager Performance table is
    # rendered from `profile.history`, so update the current-GW row there too.
    # This is separate from `selected_history`: fixing only selected_history
    # changes the summary cards but leaves the history table showing the old
    # score (for example 15 instead of the league score 50).
    if league_row is not None and current_gw == event:
        corrected_history = []
        found_current = False

        for h in history_rows:
            h = dict(h)
            if h.get("event") == event:
                h["points"] = league_row.get("event_total", h.get("points", 0))
                h["total_points"] = league_row.get("total", h.get("total_points", 0))
                if league_row.get("overall_rank") is not None:
                    h["overall_rank"] = league_row.get("overall_rank")
                found_current = True
            corrected_history.append(h)

        # If FPL's history endpoint does not contain the current GW yet, add
        # a current-GW row from the league standings so the table still shows
        # the live Achilles score.
        if not found_current:
            corrected_history.append({
                "event": event,
                "points": league_row.get("event_total", 0),
                "total_points": league_row.get("total", 0),
                "overall_rank": league_row.get("overall_rank"),
                "event_transfers": 0,
                "event_transfers_cost": 0,
            })

        history_rows = sorted(
            corrected_history,
            key=lambda x: x.get("event", 0),
            reverse=True,
        )

    # The picks endpoint may include a selected-GW entry_history object. It
    # is preferable to the longer-lived history endpoint for the active GW.
    picks_history = picks_data.get("entry_history")
    if isinstance(picks_history, dict) and picks_history.get("event") == event:
        selected_history = dict(picks_history)
    else:
        selected_history = next(
            (h for h in history_rows if h.get("event") == event),
            None,
        )

    # CRITICAL FIX:
    # During a live/provisional GW, entry/{id}/history/ can show an older GW
    # score (e.g. 15) even though the classic league standings already show
    # the current score (e.g. 50). For the active GW, the league row is the
    # authoritative source for GW total and overall total/rank.
    if league_row is not None and current_gw == event:
        if selected_history is None:
            selected_history = {"event": event}

        if league_row.get("event_total") is not None:
            selected_history["points"] = league_row.get("event_total")

        if league_row.get("total") is not None:
            selected_history["total_points"] = league_row.get("total")

        # Keep the overall rank if the league row provides it; otherwise keep
        # the rank from entry_history/history.
        if league_row.get("overall_rank") is not None:
            selected_history["overall_rank"] = league_row.get("overall_rank")

    if selected_history:
        gw_points = selected_history.get("points", 0)
        overall_points = selected_history.get("total_points", 0)
        overall_rank = selected_history.get("overall_rank")
    else:
        gw_points = sum(p.get("effective_points", 0) for p in starting)
        overall_points = manager_info.get("summary_overall_points", 0)
        overall_rank = manager_info.get("summary_overall_rank")

    transfers = selected_history.get("event_transfers", 0) if selected_history else 0
    transfer_cost = (
        selected_history.get("event_transfers_cost", 0)
        if selected_history else 0
    )

    if selected_history is not None:
        selected_history = dict(selected_history)
        selected_history.setdefault(
            "transfers", selected_history.get("event_transfers", 0)
        )

    return {
        "manager_info": manager_info,
        "picks": picks,
        "starting": starting,
        "bench": bench,
        "history": history_rows,
        "selected_history": selected_history,
        "event": event,
        "gw_points": gw_points,
        "gw_total": gw_points,
        "overall_points": overall_points,
        "overall_rank": overall_rank,
        "manager_rank": None,
        "transfers": transfers,
        "transfer_cost": transfer_cost,
    }


async def get_manager_analytics(entry_id: int, current_gw: int, s, league_row=None):
    """Build Achilles analytics data for one manager.

    Net score follows the Achilles 8.0 rulebook:
    Net Score = Gross GW Score - Transfer Cost.
    The active GW is corrected from the live classic-league standings so the
    analytics page does not inherit the stale current-GW history value.
    """
    profile = await get_manager_profile(
        entry_id,
        current_gw,
        s,
        league_row=league_row,
        current_gw=current_gw,
    )

    manager_info = profile.get("manager_info", {})
    history = list(profile.get("history", []))
    history = sorted(history, key=lambda x: x.get("event", 0))

    rows = []
    cumulative_net = 0

    for h in history:
        gw = h.get("event")
        if not gw:
            continue

        gross = h.get("points", 0) or 0
        transfer_cost = h.get("event_transfers_cost", 0) or 0
        net = gross - transfer_cost
        cumulative_net += net

        rows.append({
            "event": gw,
            "gross_points": gross,
            "transfer_cost": transfer_cost,
            "net_points": net,
            "cumulative_net": cumulative_net,
            "overall_points": h.get("total_points", 0) or 0,
            "overall_rank": h.get("overall_rank", h.get("rank")),
            "transfers": h.get("event_transfers", 0) or 0,
        })

    return {
        "entry_id": entry_id,
        "manager_name": (
            f"{manager_info.get('player_first_name', '')} "
            f"{manager_info.get('player_last_name', '')}"
        ).strip() or "FPL Manager",
        "team_name": manager_info.get("name", ""),
        "rows": rows,
        "current_gw": current_gw,
        "current_net": rows[-1]["net_points"] if rows else 0,
        "cumulative_net": rows[-1]["cumulative_net"] if rows else 0,
        "overall_points": profile.get("overall_points", 0),
        "overall_rank": profile.get("overall_rank"),
        "manager_rank": league_row.get("rank") if league_row else None,
    }


def build_line_points(rows, width=900, height=300, pad=42):
    """Return SVG polyline points for the cumulative-net chart."""
    if not rows:
        return "", []

    values = [r["cumulative_net"] for r in rows]
    min_v = min(values)
    max_v = max(values)
    if max_v == min_v:
        max_v = min_v + 1

    chart_w = width - 2 * pad
    chart_h = height - 2 * pad
    points = []
    for i, value in enumerate(values):
        x = pad if len(values) == 1 else pad + (i / (len(values) - 1)) * chart_w
        y = pad + (max_v - value) / (max_v - min_v) * chart_h
        points.append((x, y))

    point_string = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return point_string, points


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    s = load_settings()

    try:
        data = await get_league_data(s)
        error = None
    except Exception as e:
        data = {
            "standings": [],
            "league_name": s["league_name"],
            "current_gw": None,
            "current_gw_name": "—",
            "gw_winner": None,
            "highest": None,
            "lowest": None,
            "lowest_manager": None,
        }
        error = str(e)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "s": s,
            "error": error,
            **data,
        },
    )


@app.get("/league", response_class=HTMLResponse)
async def league(request: Request):
    s = load_settings()

    try:
        data = await get_league_data(s)
        error = None
    except Exception as e:
        data = {
            "standings": [],
            "league_name": s["league_name"],
            "current_gw_name": "—",
        }
        error = str(e)

    return templates.TemplateResponse(
        "league.html",
        {
            "request": request,
            "s": s,
            "error": error,
            **data,
        },
    )


@app.get("/manager/{entry_id}", response_class=HTMLResponse)
async def manager(
    request: Request,
    entry_id: int,
    gw: int | None = None,
):
    s = load_settings()

    try:
        # Determine GW from query parameter, otherwise current/next GW.
        if gw is None:
            bootstrap = await get_bootstrap()
            events = bootstrap.get("events", [])

            current_event = next(
                (e for e in events if e.get("is_current")),
                None,
            )

            if current_event is None:
                current_event = next(
                    (e for e in events if e.get("is_next")),
                    None,
                )

            event = (
                current_event.get("id")
                if current_event
                else 1
            )
        else:
            event = gw

        # Get the manager's league row. The manager template uses this
        # for name, team, league rank, GW points and overall points.
        league_data = await get_league_data(s)
        row = next(
            (r for r in league_data.get("standings", [])
             if r.get("entry") == entry_id),
            None,
        )

        if row is None:
            raise ValueError("Manager was not found in the configured league.")

        profile = await get_manager_profile(
            entry_id,
            event,
            s,
            league_row=row,
            current_gw=league_data.get("current_gw"),
        )

        # League rank comes from the configured Achilles classic league.
        profile["manager_rank"] = row.get("rank")

        # Keep both the nested `profile` object and its individual keys.
        # The existing manager.html expects profile.selected_history,
        # while other templates/code may use the individual values.
        error = None

    except Exception as e:
        profile = {
            "manager_info": {},
            "picks": [],
            "starting": [],
            "bench": [],
            "history": [],
            "selected_history": None,
            "event": gw or 1,
            "gw_points": 0,
            "overall_points": 0,
            "overall_rank": None,
            "manager_rank": None,
            "transfers": 0,
            "transfer_cost": 0,
        }
        row = None
        league_data = {"league_name": s["league_name"], "current_gw": None}
        error = str(e)

    return templates.TemplateResponse(
        "manager.html",
        {
            "request": request,
            "s": s,
            "error": error,
            "league_name": league_data.get("league_name", s["league_name"]),
            "row": row,
            "entry_id": entry_id,
            "profile": profile,
            "event": event,
            "current_gw": league_data.get("current_gw"),
            **profile,
        },
    )


@app.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request, manager: int | None = None):
    s = load_settings()
    error = None
    analytics_data = None
    standings = []

    try:
        league_data = await get_league_data(s)
        standings = league_data.get("standings", [])
        current_gw = league_data.get("current_gw") or 1

        if not standings:
            raise ValueError("No managers found in the configured league.")

        selected_entry = manager
        if selected_entry is None or not any(
            r.get("entry") == selected_entry for r in standings
        ):
            selected_entry = standings[0].get("entry")

        league_row = next(
            r for r in standings if r.get("entry") == selected_entry
        )

        analytics_data = await get_manager_analytics(
            selected_entry,
            current_gw,
            s,
            league_row=league_row,
        )

        point_string, chart_points = build_line_points(
            analytics_data["rows"]
        )
        analytics_data["chart_points"] = point_string
        analytics_data["chart_points_list"] = chart_points

        values = [r["cumulative_net"] for r in analytics_data["rows"]]
        analytics_data["chart_min"] = min(values) if values else 0
        analytics_data["chart_max"] = max(values) if values else 0

    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "s": s,
            "error": error,
            "standings": standings,
            "analytics": analytics_data,
        },
    )


async def build_comparison_metrics(entry_id: int, event: int, s, league_row=None, current_gw=None):
    """Return manager-level player-event metrics for one Gameweek.

    Metrics are calculated from the manager's actual FPL picks and the
    official event/{gw}/live/ player statistics. Captain/Vice-Captain Points
    are the actual contribution after the FPL multiplier.
    """
    profile = await get_manager_profile(
        entry_id,
        event,
        s,
        league_row=league_row,
        current_gw=current_gw,
    )

    picks = profile.get("picks", [])

    # Playing XI is positions 1-11. The optional full-squad view includes
    # positions 1-15, while captain/vice-captain status is retained.
    metric_picks = [
        p for p in picks
        if 1 <= int(p.get("squad_position") or 99) <= 11
    ]

    def n(stats, key):
        try:
            return int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    goals = sum(n(p.get("stats", {}), "goals_scored") for p in metric_picks)
    assists = sum(n(p.get("stats", {}), "assists") for p in metric_picks)
    clean_sheets = sum(n(p.get("stats", {}), "clean_sheets") for p in metric_picks)
    yellow_cards = sum(n(p.get("stats", {}), "yellow_cards") for p in metric_picks)
    red_cards = sum(n(p.get("stats", {}), "red_cards") for p in metric_picks)
    bonus = sum(n(p.get("stats", {}), "bonus") for p in metric_picks)

    captain_points = sum(
        int(p.get("effective_points", 0) or 0)
        for p in picks
        if p.get("is_captain")
    )
    vice_captain_points = sum(
        int(p.get("effective_points", 0) or 0)
        for p in picks
        if p.get("is_vice_captain")
    )

    return {
        "goals": goals,
        "assists": assists,
        "clean_sheets": clean_sheets,
        "yellow_cards": yellow_cards,
        "red_cards": red_cards,
        "bonus": bonus,
        "captain_points": captain_points,
        "vice_captain_points": vice_captain_points,
        "gw_points": int(profile.get("gw_points", 0) or 0),
        "total_points": int(profile.get("overall_points", 0) or 0),
        "overall_rank": profile.get("overall_rank"),
        "picks": picks,
    }



# ============================================================
# LAST MAN STANDING (LMS)
# ============================================================

# User instruction for Achilles 8.0 implementation: LMS starts at GW1.
# One manager is eliminated in every available Gameweek.
LMS_START_GW = 1
LMS_LAST_ELIMINATION_GW = 37


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _lowest_label(position: int, joint: bool = False) -> str:
    if joint and position == 1:
        return "Joint lowest"
    if position == 1:
        return "Lowest"
    prefix = "Joint " if joint else ""
    return f"{prefix}{_ordinal(position)} lowest"


async def _lms_manager_gw(entry_id: int, gw: int, live_map: dict, standings_by_entry: dict):
    """Return LMS metrics for one manager in one Gameweek.

    Metrics are based on the manager's Playing XI.  Gross GW points and
    transfer cost come from the manager's Gameweek entry_history where
    available.  For the active/current GW, the classic-league standings are
    used as the authoritative GW score and transfer cost remains sourced from
    entry_history when available.
    """
    picks_data = await fpl_get(
        f"entry/{entry_id}/event/{gw}/picks/",
        0 if gw == standings_by_entry.get(entry_id, {}).get("_current_gw") else 300,
    )
    picks = picks_data.get("picks", [])
    entry_history = picks_data.get("entry_history") or {}

    def n(stats, key):
        try:
            return int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    starters = [
        p for p in picks
        if 1 <= int(p.get("position") or 99) <= 11
    ]

    goals = assists = clean_sheets = bonus = yellow = red = 0
    captain_points = vice_captain_points = 0

    for pick in starters:
        stats = live_map.get(pick.get("element"), {})
        goals += n(stats, "goals_scored")
        assists += n(stats, "assists")
        clean_sheets += n(stats, "clean_sheets")
        bonus += n(stats, "bonus")
        yellow += n(stats, "yellow_cards")
        red += n(stats, "red_cards")

    for pick in picks:
        stats = live_map.get(pick.get("element"), {})
        points = n(stats, "total_points")
        multiplier = int(pick.get("multiplier", 0) or 0)
        if pick.get("is_captain"):
            captain_points = points * multiplier
        if pick.get("is_vice_captain"):
            vice_captain_points = points * max(multiplier, 1)

    league_row = standings_by_entry.get(entry_id, {})
    gross = entry_history.get("points")

    # Current GW league standings are authoritative when they contain a GW
    # score; historical GWs use entry_history.
    if league_row.get("_current_gw") == gw and league_row.get("event_total") is not None:
        gross = league_row.get("event_total")

    if gross is None:
        gross = sum(
            n(live_map.get(p.get("element"), {}), "total_points")
            * int(p.get("multiplier", 0) or 0)
            for p in picks
        )

    transfer_cost = int(entry_history.get("event_transfers_cost", 0) or 0)
    net = int(gross or 0) - transfer_cost

    league_row = standings_by_entry.get(entry_id, {})

    return {
        "entry": entry_id,
        "player_name": league_row.get("player_name", "—"),
        "entry_name": league_row.get("entry_name", ""),
        "gw_points": int(gross or 0),
        "transfer_cost": transfer_cost,
        "net_score": net,
        "goals": goals,
        "assists": assists,
        "clean_sheets": clean_sheets,
        "bonus": bonus,
        "yellow_cards": yellow,
        "red_cards": red,
        "captain_points": captain_points,
        "vice_captain_points": vice_captain_points,
    }


async def _get_lms_completed_gws(events, current_gw):
    """Return Gameweeks that are safe to use for LMS.

    Normally the FPL bootstrap event's ``finished`` flag is authoritative.
    Around the end of a Gameweek, however, that flag can briefly lag behind
    the fixture data. For LMS we therefore also verify the current Gameweek's
    fixtures directly. A Gameweek is considered complete when every fixture
    assigned to that Gameweek is marked finished.
    """
    completed = {
        int(e.get("id"))
        for e in events
        if e.get("id") is not None and e.get("finished") is True
    }

    if current_gw:
        try:
            fixtures = await fpl_get(f"fixtures/?event={int(current_gw)}", 0)
            gw_fixtures = [
                f for f in fixtures
                if int(f.get("event") or 0) == int(current_gw)
            ]
            if gw_fixtures and all(f.get("finished") is True for f in gw_fixtures):
                completed.add(int(current_gw))
        except Exception:
            # If fixture verification is temporarily unavailable, retain the
            # normal bootstrap-based completion status rather than failing LMS.
            pass

    return completed


async def build_lms_table(s):
    """Build the LMS elimination history from GW1 onward.

    The important distinction from a simple 'lowest among survivors' sort is
    that the displayed GW position is the manager's position among ALL
    managers for that GW.  Therefore, if the 1st/2nd/3rd lowest scorers have
    already been eliminated, the next active manager can be eliminated as the
    4th lowest, matching the historical LMS spreadsheet logic.

    Primary criterion:
        lowest Net Score = GW Points - Transfer Cost

    Tie-break hierarchy supplied by the user for this implementation:
        1. Most goals
        2. Most assists
        3. Most clean sheets
        4. Most bonus points
        5. Fewer yellow cards
        6. Fewer red cards
        7. Most captaincy points
        8. Most vice-captaincy points
        9. Lowest transfer cost / lowest negative transfer impact

    Only one active manager is eliminated per available GW.
    """
    league_data = await get_league_data(s)
    standings = league_data.get("standings", [])
    current_gw = league_data.get("current_gw")

    if not standings:
        return {
            "current_gw": current_gw,
            "rows": [],
            "survivors": [],
            "started": False,
            "message": "No managers were found in the configured league.",
        }

    survivors = {
        int(r.get("entry")): {
            "entry": int(r.get("entry")),
            "player_name": r.get("player_name", "—"),
            "entry_name": r.get("entry_name", ""),
        }
        for r in standings
        if r.get("entry") is not None
    }

    if not current_gw or current_gw < LMS_START_GW:
        return {
            "current_gw": current_gw,
            "rows": [],
            "survivors": list(survivors.values()),
            "started": False,
            "message": "LMS starts from GW1. No available Gameweek data has been loaded yet.",
        }

    # Use a fresh bootstrap read here because this page is specifically used
    # immediately after a Gameweek finishes.
    bootstrap = await fpl_get("bootstrap-static/", 0)
    events = bootstrap.get("events", [])

    # A short API propagation delay can leave event.finished=False even after
    # all matches have ended. Verify the current GW at fixture level as well.
    completed_gws = await _get_lms_completed_gws(events, current_gw)

    last_completed_gw = max(completed_gws, default=0)
    last_gw = min(last_completed_gw, LMS_LAST_ELIMINATION_GW)
    rows = []
    lms_warning = None

    # Keep all managers in this map. We intentionally do NOT remove eliminated
    # managers from the GW ranking calculation because the elimination reason
    # must say 2nd/3rd/4th lowest when lower-ranked managers are already out.
    all_entries = [int(r.get("entry")) for r in standings if r.get("entry") is not None]
    standings_by_entry = {
        int(r.get("entry")): dict(r)
        for r in standings
        if r.get("entry") is not None
    }
    for entry_id in all_entries:
        standings_by_entry[entry_id]["_current_gw"] = current_gw

    for gw in range(LMS_START_GW, last_gw + 1):
        # One live endpoint per GW.  Only officially finished Gameweeks reach
        # this point, so LMS never uses in-progress scores.
        try:
            live_ttl = 0 if gw == last_gw else 300
            live_data = await fpl_get(f"event/{gw}/live/", live_ttl)
        except httpx.HTTPStatusError as exc:
            # If a GW is not available yet, keep all previously calculated LMS
            # rows and stop. Do not make the whole LMS tab fail.
            if exc.response.status_code == 404:
                break
            lms_warning = f"FPL API temporarily unavailable while loading GW{gw}. LMS has not calculated that Gameweek yet."
            break
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            lms_warning = f"FPL API connection timed out while loading GW{gw}. LMS has not calculated that Gameweek yet."
            break

        live_map = {
            item.get("id"): item.get("stats", {})
            for item in live_data.get("elements", [])
        }

        results = await asyncio.gather(
            *[
                _lms_manager_gw(entry_id, gw, live_map, standings_by_entry)
                for entry_id in all_entries
            ],
            return_exceptions=True,
        )

        # A single unavailable picks endpoint means the GW is not sufficiently
        # complete to perform an official LMS elimination. Stop at that GW.
        exceptions = [result for result in results if isinstance(result, Exception)]
        if exceptions:
            unavailable = exceptions[0]
            if isinstance(unavailable, httpx.HTTPStatusError) and unavailable.response.status_code == 404:
                break

            if isinstance(unavailable, (httpx.TimeoutException, httpx.NetworkError)):
                lms_warning = f"FPL API connection problem while loading manager data for GW{gw}. LMS has not calculated that Gameweek yet."
                break

            if isinstance(unavailable, httpx.HTTPStatusError):
                lms_warning = f"FPL API returned an error while loading manager data for GW{gw}. LMS has not calculated that Gameweek yet."
                break

            # Do not allow one unexpected manager/API response to blank the
            # entire LMS page. Preserve all previously calculated GWs.
            lms_warning = f"LMS could not complete GW{gw} because manager data was temporarily unavailable."
            break

        metrics_by_entry = {
            result["entry"]: dict(result)
            for result in results
        }

        # Full GW ranking: lowest net first, with the tie-break hierarchy used
        # to order managers sharing the same net score.
        sort_spec = [
            ("net_score", False),
            ("goals", True),
            ("assists", True),
            ("clean_sheets", True),
            ("bonus", True),
            ("yellow_cards", False),
            ("red_cards", False),
            ("captain_points", True),
            ("vice_captain_points", True),
            ("transfer_cost", False),
        ]

        ranked = list(metrics_by_entry.values())
        for key, descending in reversed(sort_spec):
            ranked.sort(key=lambda x: x.get(key, 0), reverse=descending)

        # Competition position is based on Net Score only. Tied net scores
        # share the same position; tie-breaks decide which tied manager is
        # actually eliminated.
        position_by_entry = {}
        i = 0
        while i < len(ranked):
            net = ranked[i]["net_score"]
            j = i
            while j < len(ranked) and ranked[j]["net_score"] == net:
                j += 1
            for k in range(i, j):
                position_by_entry[ranked[k]["entry"]] = i + 1
            i = j

        # Select the first manager in the full GW ranking who is still alive.
        eliminated = next(
            item for item in ranked
            if item["entry"] in survivors
        )
        eliminated_entry = eliminated["entry"]
        gw_position = position_by_entry[eliminated_entry]

        # Tie-break only between ACTIVE managers at the qualifying net score.
        # Managers already eliminated can still determine the displayed GW
        # position, but they cannot win/lose the current tie-break again.
        active_same_net = [
            item for item in ranked
            if item["net_score"] == eliminated["net_score"]
            and item["entry"] in survivors
        ]

        has_tie = len(active_same_net) > 1
        reason = f"{_lowest_label(gw_position, joint=has_tie)} in the GW"
        if gw_position > 1:
            reason += ", rest already eliminated"

        if has_tie:
            tie_break_labels = [
                ("goals", "Goals", True),
                ("assists", "Assists", True),
                ("clean_sheets", "Clean Sheets", True),
                ("bonus", "Bonus Points", True),
                ("yellow_cards", "Yellow Cards", False),
                ("red_cards", "Red Cards", False),
                ("captain_points", "Captain Points", True),
                ("vice_captain_points", "Vice-Captain Points", True),
                ("transfer_cost", "Transfer Cost", False),
            ]

            # Only explain the first hierarchy level that separates the tied
            # managers. This mirrors the requested tie-break documentation.
            for key, label, higher_is_better in tie_break_labels:
                values = [item.get(key, 0) for item in active_same_net]
                if len(set(values)) > 1:
                    chosen_value = eliminated.get(key, 0)
                    direction = "most" if higher_is_better else "fewest"
                    reason += f" — eliminated on tie-break: {direction} {label} ({chosen_value})"
                    break
            else:
                reason += " — all tie-break criteria equal"

        rows.append({
            "gw": gw,
            "entry": eliminated_entry,
            "player_name": eliminated.get("player_name") or survivors[eliminated_entry].get("player_name", "—"),
            "entry_name": eliminated.get("entry_name") or survivors[eliminated_entry].get("entry_name", ""),
            "position": gw_position,
            "gw_points": eliminated["gw_points"],
            "net_score": eliminated["net_score"],
            "reason": reason,
            "goals": eliminated["goals"],
            "assists": eliminated["assists"],
            "clean_sheets": eliminated["clean_sheets"],
            "bonus": eliminated["bonus"],
            "yellow_cards": eliminated["yellow_cards"],
            "red_cards": eliminated["red_cards"],
            "captain_points": eliminated["captain_points"],
            "vice_captain_points": eliminated["vice_captain_points"],
            "transfer_cost": eliminated["transfer_cost"],
            "survivors_after": len(survivors) - 1,
        })

        del survivors[eliminated_entry]

        # Once only two managers remain, they are the finalists. They are not
        # eliminated in GW38; GW38 is their final head-to-head decider.
        if len(survivors) <= 2:
            break

    survivor_list = sorted(
        survivors.values(),
        key=lambda x: x.get("player_name", "").lower(),
    )

    # LMS is driven by the latest completed Gameweek, not by whether the
    # next/current Gameweek is still marked as live.
    current_event = next(
        (e for e in events if int(e.get("id") or 0) == int(current_gw or 0)),
        None,
    )
    current_finished = bool(current_event and current_event.get("finished") is True)
    latest_completed_gw = max(completed_gws, default=0)

    if lms_warning:
        message = lms_warning
    elif rows:
        message = f"LMS updated through GW{min(latest_completed_gw, LMS_LAST_ELIMINATION_GW)}."
    else:
        message = "No completed Gameweek data is available for LMS yet."

    return {
        "current_gw": current_gw,
        "rows": rows,
        "survivors": survivor_list,
        "started": bool(rows),
        "message": message,
        "current_finished": current_finished,
        "manually_ended_gw": s.get("lms_gw_ended"),
    }


@app.get("/lms", response_class=HTMLResponse)
async def lms(request: Request):
    s = load_settings()
    cached_data = load_lms_cache()
    try:
        data = await build_lms_table(s)
        error = None

        # Save every usable result. This means a later browser refresh can
        # safely fall back to the last working LMS table if the FPL API is
        # temporarily slow/unreachable.
        if data.get("rows") or data.get("survivors"):
            save_lms_cache(data)
    except Exception as e:
        # NEVER replace a previously working LMS page with an empty page just
        # because the FPL API timed out during this refresh.
        if cached_data:
            data = cached_data
            error = None
            data["message"] = (
                data.get("message", "LMS data loaded from the last successful refresh.")
                + " FPL API was temporarily unavailable; showing the last successful LMS data."
            )
        else:
            data = {
                "current_gw": None,
                "rows": [],
                "survivors": [],
                "started": False,
                "message": "LMS data is temporarily unavailable. Please refresh again in a moment.",
            }
            error = None

    return templates.TemplateResponse(
        "lms_v3.html",
        {
            "request": request,
            "s": s,
            "error": error,
            **data,
        },
    )


@app.get("/competitions/lms", response_class=HTMLResponse)
async def competition_lms(request: Request):
    return await lms(request)


@app.get("/comparison", response_class=HTMLResponse)
async def comparison(
    request: Request,
    managers: str = "",
    gw: int | None = None,
):
    s = load_settings()
    error = None
    standings = []
    selected = []
    selected_ids = []
    comparison_metrics = []
    available_gws = []
    current_gw = None
    current_gw_name = "—"
    selected_gw = None
    data_unavailable = None

    try:
        league_data = await get_league_data(s)
        standings = league_data.get("standings", [])
        current_gw = league_data.get("current_gw")
        current_gw_name = league_data.get("current_gw_name", "—")

        # The FPL bootstrap provides the complete list of Gameweeks. Only
        # completed/current/available events are offered to the user.
        bootstrap = await get_bootstrap()
        events = bootstrap.get("events", [])
        available_gws = [
            int(e.get("id"))
            for e in events
            if e.get("id") is not None
            and (e.get("finished") or e.get("is_current"))
        ]
        available_gws = sorted(set(available_gws))

        if not available_gws:
            available_gws = [current_gw] if current_gw else [1]

        selected_gw = int(gw) if gw in available_gws else (current_gw or available_gws[-1])

        # The comparison form sends manager entry IDs as a comma-separated
        # query parameter. Keep the selection capped at 10 on the server too.
        requested_ids = []
        for value in managers.split(","):
            value = value.strip()
            if value.isdigit():
                requested_ids.append(int(value))

        for entry_id in requested_ids[:10]:
            if entry_id not in selected_ids:
                row = next(
                    (r for r in standings if r.get("entry") == entry_id),
                    None,
                )
                if row is not None:
                    selected_ids.append(entry_id)
                    selected.append(dict(row))

        # For the selected GW, use the manager's FPL history/picks rather
        # than the current classic-league standings for historical totals.
        for row in selected:
            entry_id = int(row.get("entry"))
            try:
                metrics = await build_comparison_metrics(
                    entry_id,
                    selected_gw,
                    s,
                    league_row=row,
                    current_gw=current_gw,
                )

                # For historical GWs, get_manager_profile uses entry history for
                # GW points, overall points and overall rank. For the active GW,
                # it uses the live classic-league row as the authoritative source.
                row["comparison_gw_points"] = metrics["gw_points"]
                row["comparison_total_points"] = metrics["total_points"]
                row["comparison_overall_rank"] = metrics["overall_rank"]
                comparison_metrics.append({
                    "entry": entry_id,
                    "player_name": row.get("player_name", "—"),
                    "gw_points": metrics["gw_points"],
                    "total_points": metrics["total_points"],
                    "overall_rank": metrics["overall_rank"],
                    "goals": metrics["goals"],
                    "assists": metrics["assists"],
                    "clean_sheets": metrics["clean_sheets"],
                    "yellow_cards": metrics["yellow_cards"],
                    "red_cards": metrics["red_cards"],
                    "bonus": metrics["bonus"],
                    "captain_points": metrics["captain_points"],
                    "vice_captain_points": metrics["vice_captain_points"],
                    "unavailable": False,
                })
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise

                # FPL returns 404 for a manager's picks endpoint when the
                # selected Gameweek is not available yet. Treat that as a
                # normal data-availability condition instead of allowing the
                # whole Comparison page to fail with a 500 error.
                comparison_metrics.append({
                    "entry": entry_id,
                    "player_name": row.get("player_name", "—"),
                    "gw_points": None,
                    "total_points": None,
                    "overall_rank": None,
                    "goals": None,
                    "assists": None,
                    "clean_sheets": None,
                    "yellow_cards": None,
                    "red_cards": None,
                    "bonus": None,
                    "captain_points": None,
                    "vice_captain_points": None,
                    "unavailable": True,
                })
                data_unavailable = (
                    f"GW{selected_gw} data is not available from FPL yet. "
                    "The manager picks endpoint returned 404, so no player "
                    "metrics have been calculated for this Gameweek."
                )

        # Replace the current-GW-only values in the results table with the
        # selected Gameweek values, without changing the league selector table.
        metric_by_entry = {m["entry"]: m for m in comparison_metrics}
        for row in selected:
            m = metric_by_entry.get(row.get("entry"), {})
            if m:
                row["event_total"] = m["gw_points"]
                row["total"] = m["total_points"]
                row["overall_rank"] = m["overall_rank"]

    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        "comparison_v2.html",
        {
            "request": request,
            "s": s,
            "error": error,
            "standings": standings,
            "selected": selected,
            "selected_ids": selected_ids,
            "current_gw_name": current_gw_name,
            "current_gw": current_gw,
            "selected_gw": selected_gw,
            "available_gws": available_gws,
            "comparison_metrics": comparison_metrics,
            "data_unavailable": data_unavailable,
        },
    )


@app.get("/competitions/comparison", response_class=HTMLResponse)
async def competition_comparison(
    request: Request, managers: str = "", gw: int | None = None
):
    return await comparison(request, managers, gw)


@app.get("/european", response_class=HTMLResponse)
@app.get("/competitions/european", response_class=HTMLResponse)
async def european_qualifiers(request: Request):
    return await render_european(request, "european")


async def render_european(request: Request, name: str):
    s = load_settings()
    labels = {
        "european": "European Qualifiers",
        "ucl": "Champions League",
        "europa": "Europa League",
        "conference": "Conference League",
    }

    if name == "european":
        try:
            league_data = await get_league_data(s)
            standings = league_data.get("standings", [])
            real_groups = load_groups()
            groups = {g: list(members)[:5] for g, members in real_groups.items()}
            histories = await get_histories(groups, standings, fpl_get)
            schedule = build_schedule(groups)
            tables, fixtures = build_group_tables(groups, histories, standings, schedule)
            qualification = qualification_rows(tables)
            completed_gws = sorted({
                int(h.get("event"))
                for obj in histories.values()
                for h in obj.get("history", {}).values()
                if h.get("event") is not None
            })
            data_error = None
        except Exception as e:
            groups = {g: [] for g in "ABCDEFGH"}
            tables = {g: [] for g in "ABCDEFGH"}
            fixtures = []
            qualification = []
            completed_gws = []
            data_error = str(e)

        return templates.TemplateResponse(
            "european.html",
            {
                "request": request, "s": s, "error": data_error,
                "name": "european", "title": labels["european"],
                "groups": groups, "tables": tables, "fixtures": fixtures,
                "qualification": qualification, "knockout": [],
                "completed_gws": completed_gws,
            },
        )

    knockout = {
        "ucl": [
            ("Round of 16", [26, 28], 8),
            ("Quarter Finals", [30, 32], 4),
            ("Semi Finals", [34, 36], 2),
            ("Final", [38], 1),
        ],
        "europa": [
            ("Round of 16", [26, 28], 8),
            ("Quarter Finals", [30, 32], 4),
            ("Semi Finals", [34, 36], 2),
            ("Final", [38], 1),
        ],
        "conference": [
            ("Quarter Finals", [30, 32], 4),
            ("Semi Finals", [34, 36], 2),
            ("Final", [38], 1),
        ],
    }[name]

    return templates.TemplateResponse(
        "european.html",
        {
            "request": request, "s": s, "error": None,
            "name": name, "title": labels[name],
            "groups": {}, "tables": {}, "fixtures": [],
            "qualification": [], "knockout": knockout, "completed_gws": [],
        },
    )



# ============================================================
# PRIZE CENTRE — ACHILLES 8.0 CUP
# ============================================================
GW_PRIZES = {1: 170, 2: 140, 3: 120, 4: 90, 5: 70}
MOTM_PRIZE = 380
MOTM_PERIODS = [
    (1, "Aug / Sep", 1, 5), (2, "October", 6, 9),
    (3, "November", 10, 13), (4, "December", 14, 18),
    (5, "January", 19, 22), (6, "February", 23, 26),
    (7, "March", 27, 30), (8, "April", 31, 35),
    (9, "May", 36, 38),
]
LMS_PRIZES = {1: 1500, 2: 900, 3: 700, 4: 500, 5: 400, 6: 300}
FINAL_STANDING_PRIZES = {
    1: 5360, 2: 4700, 3: 4150, 4: 3650, 5: 3100,
    6: 2600, 7: 2000, 8: 1500, 9: 1000, 10: 700,
}
EURO_PRIZES = {
    "Champions League": {"Winner": 1900, "2nd place": 1200, "3rd place": 500},
    "Europa League": {"Winner": 1500, "2nd place": 750, "3rd place": 300},
    "Conference League": {"Winner": 1000, "2nd place": 500, "3rd place": 250},
}
EURO_GROUP_WINNER_PRIZE = 150
FINAL_STANDING_PRINTED_TOTAL = sum(FINAL_STANDING_PRIZES.values())
PRIZE_TIE_BREAKERS = [
    ("goals", "Goals", True),
    ("assists", "Assists", True),
    ("clean_sheets", "Clean Sheets", True),
    ("bonus", "Bonus Points", True),
    ("yellow_cards", "Yellow Cards", False),
    ("red_cards", "Red Cards", False),
    ("captain_points", "Captain Points", True),
    ("vice_captain_points", "Vice-Captain Points", True),
    ("transfer_cost", "Transfer Cost", False),
]

def _history_by_gw(history_data):
    return {int(h.get("event")): h for h in (history_data.get("current", []) or []) if h.get("event") is not None}

async def _load_prize_histories(standings):
    semaphore = asyncio.Semaphore(10)
    async def one(row):
        entry = row.get("entry")
        async with semaphore:
            try:
                return entry, await fpl_get(f"entry/{entry}/history/", 60)
            except Exception:
                return entry, {}
    return dict(await asyncio.gather(*[one(r) for r in standings if r.get("entry") is not None]))

async def _manager_gw_tiebreak_metrics(entry_id, gw, live_map):
    picks_data = await fpl_get(f"entry/{entry_id}/event/{gw}/picks/", 300)
    picks = picks_data.get("picks", []) or []

    def n(stats, key):
        try:
            return int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    starters = [
        p for p in picks
        if 1 <= int(p.get("position") or 99) <= 11
    ]

    metrics = {
        "goals": sum(
            n(live_map.get(p.get("element"), {}), "goals_scored")
            for p in starters
        ),
        "assists": sum(
            n(live_map.get(p.get("element"), {}), "assists")
            for p in starters
        ),
        "clean_sheets": sum(
            n(live_map.get(p.get("element"), {}), "clean_sheets")
            for p in starters
        ),
        "bonus": sum(
            n(live_map.get(p.get("element"), {}), "bonus")
            for p in starters
        ),
        "yellow_cards": sum(
            n(live_map.get(p.get("element"), {}), "yellow_cards")
            for p in starters
        ),
        "red_cards": sum(
            n(live_map.get(p.get("element"), {}), "red_cards")
            for p in starters
        ),
        "captain_points": 0,
        "vice_captain_points": 0,
    }

    for p in picks:
        stats = live_map.get(p.get("element"), {})
        points = n(stats, "total_points")
        multiplier = int(p.get("multiplier", 0) or 0)

        if p.get("is_captain"):
            metrics["captain_points"] = points * max(multiplier, 1)

        if p.get("is_vice_captain"):
            metrics["vice_captain_points"] = points * max(multiplier, 1)

    return metrics

async def _rank_gw_prizes(gw, standings, history_by_entry):
    rows = []

    for row in standings:
        entry = row.get("entry")
        h = _history_by_gw(history_by_entry.get(entry, {})).get(gw)

        if h is None and row.get("_current_gw") == gw:
            gross = row.get("event_total", 0) or 0
            transfer_cost = 0
        elif h is None:
            continue
        else:
            gross = h.get("points", 0) or 0
            transfer_cost = h.get("event_transfers_cost", 0) or 0

            if row.get("_current_gw") == gw and row.get("event_total") is not None:
                gross = row.get("event_total")

        rows.append({
            "entry": entry,
            "player_name": row.get("player_name", "—"),
            "team_name": row.get("entry_name", ""),
            "gw_score": int(gross),
            "transfer_cost": int(transfer_cost),
            "net_score": int(gross) - int(transfer_cost),
        })

    if not rows:
        return []

    rows.sort(key=lambda x: x["net_score"], reverse=True)

    # Only managers who can affect the Top 5 need player-level tie-break data.
    cutoff = rows[min(4, len(rows) - 1)]["net_score"]
    tied = [r for r in rows if r["net_score"] >= cutoff]

    metrics = {}
    if len(tied) > 1:
        live_data = await fpl_get(f"event/{gw}/live/", 300)
        live_map = {
            x.get("id"): x.get("stats", {})
            for x in live_data.get("elements", [])
        }

        results = await asyncio.gather(
            *[
                _manager_gw_tiebreak_metrics(r["entry"], gw, live_map)
                for r in tied
            ],
            return_exceptions=True,
        )

        for r, m in zip(tied, results):
            metrics[r["entry"]] = {} if isinstance(m, Exception) else m

    for r in rows:
        r.update(metrics.get(r["entry"], {}))
        for key, _, _ in PRIZE_TIE_BREAKERS:
            r.setdefault(key, 0)

    # Apply exactly the same hierarchy as LMS.
    sort_spec = [("net_score", True)] + [
        (key, descending)
        for key, _, descending in PRIZE_TIE_BREAKERS
    ]

    for key, descending in reversed(sort_spec):
        rows.sort(
            key=lambda x: x.get(key, 0),
            reverse=descending,
        )

    # Explain the first tie-break criterion that separates managers
    # sharing the same Net Score.
    for r in rows:
        same_net = [
            item for item in rows
            if item.get("net_score") == r.get("net_score")
        ]

        r["tie_break_reason"] = "—"

        if len(same_net) > 1:
            for key, label, higher_is_better in PRIZE_TIE_BREAKERS:
                values = [item.get(key, 0) for item in same_net]

                if len(set(values)) > 1:
                    value = r.get(key, 0)
                    direction = "Most" if higher_is_better else "Fewest"
                    r["tie_break_reason"] = f"{direction} {label} ({value})"
                    break
            else:
                r["tie_break_reason"] = "All tie-break criteria equal"

    for idx, r in enumerate(rows[:5], 1):
        r.update(rank=idx, prize=GW_PRIZES[idx])

    return rows[:5]

async def _motm_period_result(period, standings, history_by_entry):
    _, name, start_gw, end_gw = period
    candidates = []
    for row in standings:
        by_gw = _history_by_gw(history_by_entry.get(row.get("entry"), {}))
        period_rows = [by_gw[g] for g in range(start_gw, end_gw+1) if g in by_gw]
        if not period_rows: continue
        candidates.append({
            "entry": row.get("entry"), "player_name": row.get("player_name", "—"),
            "net_score": sum(int(h.get("points", 0) or 0) - int(h.get("event_transfers_cost", 0) or 0) for h in period_rows),
            "gws_available": len(period_rows),
        })
    if not candidates:
        return {"period": name, "gw_range": f"GW{start_gw}–GW{end_gw}", "winner": None, "net_score": None, "complete": False, "status": "Pending"}
    complete = all(c["gws_available"] == end_gw-start_gw+1 for c in candidates)
    best = max(c["net_score"] for c in candidates)
    tied = [c for c in candidates if c["net_score"] == best]
    # Tie-break metrics are only fetched if MOTM is genuinely tied.
    if len(tied) > 1 and complete:
        for c in tied:
            for key, _, _ in PRIZE_TIE_BREAKERS: c[key] = 0
        for gw in range(start_gw, end_gw+1):
            live = await fpl_get(f"event/{gw}/live/", 300)
            live_map = {x.get("id"): x.get("stats", {}) for x in live.get("elements", [])}
            for c in tied:
                try: m = await _manager_gw_tiebreak_metrics(c["entry"], gw, live_map)
                except Exception: m = {}
                for key, _, _ in PRIZE_TIE_BREAKERS:
                    if key == "transfer_cost":
                        h = _history_by_gw(history_by_entry.get(c["entry"], {})).get(gw, {})
                        c[key] += int(h.get("event_transfers_cost", 0) or 0)
                    else: c[key] += m.get(key, 0)
        for key, _, descending in reversed(PRIZE_TIE_BREAKERS):
            tied.sort(key=lambda x: x.get(key, 0), reverse=descending)
    winner = tied[0]
    return {"period": name, "gw_range": f"GW{start_gw}–GW{end_gw}", "winner": winner["player_name"],
            "entry": winner["entry"], "net_score": winner["net_score"], "complete": complete,
            "status": "Winner" if complete else "Current leader"}

async def build_prize_centre(s, selected_gw=None, selected_period=1):
    league_data = await get_league_data(s)
    standings = league_data.get("standings", [])
    current_gw = league_data.get("current_gw")
    for row in standings: row["_current_gw"] = current_gw
    history_by_entry = await _load_prize_histories(standings)
    available_gws = sorted({int(h.get("event")) for obj in history_by_entry.values() for h in (obj.get("current", []) or []) if h.get("event") is not None and 1 <= int(h.get("event")) <= 38})
    if current_gw and current_gw not in available_gws: available_gws.append(current_gw)
    available_gws = sorted(set(available_gws))
    if selected_gw not in available_gws: selected_gw = current_gw if current_gw in available_gws else (available_gws[-1] if available_gws else 1)
    gw_rows = await _rank_gw_prizes(selected_gw, standings, history_by_entry)
    motm_results = [await _motm_period_result(p, standings, history_by_entry) for p in MOTM_PERIODS]
    final_rows = [{"rank": r.get("rank"), "player_name": r.get("player_name", "—"), "team_name": r.get("entry_name", ""), "total": r.get("total", 0), "prize": FINAL_STANDING_PRIZES.get(r.get("rank"), 0)} for r in sorted(standings, key=lambda r: r.get("rank") or 999999)[:10]]
    earnings = {r.get("entry"): {"entry": r.get("entry"), "manager": r.get("player_name", "—"), **{f"gw{g}": 0 for g in range(1,39)}, "overall": 0} for r in standings}
    for gw in available_gws:
        for r in await _rank_gw_prizes(gw, standings, history_by_entry):
            if r["entry"] in earnings: earnings[r["entry"]][f"gw{gw}"] += r["prize"]
    period_end = {p[1]: p[3] for p in MOTM_PERIODS}
    for result in motm_results:
        if result.get("complete") and result.get("entry") in earnings:
            earnings[result["entry"]][f"gw{period_end[result['period']]}"] += MOTM_PRIZE
    for e in earnings.values(): e["overall"] = sum(e[f"gw{g}"] for g in range(1,39))
    return {
        "league_name": league_data.get("league_name"), "current_gw": current_gw, "available_gws": available_gws, "selected_gw": selected_gw,
        "gw_rows": gw_rows, "motm_results": motm_results, "selected_period": selected_period,
        "lms_prizes": [{"rank": k, "prize": v} for k,v in LMS_PRIZES.items()], "euro_group_prize": EURO_GROUP_WINNER_PRIZE, "euro_prizes": EURO_PRIZES,
        "final_rows": final_rows, "final_prizes": [{"rank": k, "prize": v} for k,v in FINAL_STANDING_PRIZES.items()], "final_standing_printed_total": FINAL_STANDING_PRINTED_TOTAL,
        "earnings_rows": sorted(earnings.values(), key=lambda x: x["manager"].lower()),
        "totals": {"entry_fee":1700,"participants":40,"pot":68000,"gw":22420,"lms":4300,"motm":3420,"europe":9100,"final":FINAL_STANDING_PRINTED_TOTAL},
    }

@app.get("/prizes", response_class=HTMLResponse)
@app.get("/competitions/prizes", response_class=HTMLResponse)
async def prizes(request: Request, gw: int | None = None, period: int = 1):
    s = load_settings()
    try:
        data = await build_prize_centre(s, gw, period); error = None
    except Exception as e:
        data = {"league_name":s.get("league_name","Achilles 8.0"),"current_gw":None,"available_gws":[],"selected_gw":gw or 1,"gw_rows":[],"motm_results":[],"selected_period":period,
                "lms_prizes":[{"rank":k,"prize":v} for k,v in LMS_PRIZES.items()],"euro_group_prize":EURO_GROUP_WINNER_PRIZE,"euro_prizes":EURO_PRIZES,"final_rows":[],
                "final_prizes":[{"rank":k,"prize":v} for k,v in FINAL_STANDING_PRIZES.items()],"final_standing_printed_total":FINAL_STANDING_PRINTED_TOTAL,"earnings_rows":[],
                "totals":{"entry_fee":1700,"participants":40,"pot":68000,"gw":22420,"lms":4300,"motm":3420,"europe":9100,"final":FINAL_STANDING_PRINTED_TOTAL}}; error = str(e)
    return templates.TemplateResponse("prizes.html", {"request":request,"s":s,"title":"Prizes","error":error,**data})


@app.get("/weekly-update", response_class=HTMLResponse)
async def weekly_update(request: Request, gw: int | None = None):
    """Fast, reliable Weekly Update.

    Important:
    - Calculates only the selected Gameweek instead of rebuilding all prize/MOTM data.
    - Uses the persistent LMS cache so a temporary FPL API slowdown does not blank
      the Weekly Update page.
    - Uses the exact same tie-break hierarchy as LMS.
    """
    s = load_settings()

    error = None
    data = {
        "league_name": s.get("league_name", "Achilles 8.0"),
        "current_gw": None,
        "selected_gw": gw or 1,
        "next_gw": None,
        "available_gws": [],
        "gw_rows": [],
        "participants": 0,
        "highest_score": None,
        "highest_manager": None,
        "lowest_score": None,
        "lowest_manager": None,
        "average_score": None,
        "lms_row": None,
        "lms_survivors": [],
        "lms_started": False,
        "lms_message": None,
    }

    try:
        # One league-data call gives us the current GW and all managers.
        league_data = await get_league_data(s)
        standings = league_data.get("standings", [])
        current_gw = league_data.get("current_gw")

        # Determine which GWs are completed. Respect the same manual LMS override
        # used elsewhere in the application.
        bootstrap = await get_bootstrap()
        events = bootstrap.get("events", [])

        completed_gws = sorted(
            int(e.get("id"))
            for e in events
            if e.get("id") is not None
            and e.get("finished") is True
            and 1 <= int(e.get("id")) <= 38
        )

        manually_ended_gw = s.get("lms_gw_ended")
        try:
            manually_ended_gw = (
                int(manually_ended_gw)
                if manually_ended_gw is not None
                else None
            )
        except (TypeError, ValueError):
            manually_ended_gw = None

        if manually_ended_gw is not None and 1 <= manually_ended_gw <= 38:
            completed_gws = sorted(
                set(completed_gws) | {manually_ended_gw}
            )

        if gw is not None and int(gw) in completed_gws:
            selected_gw = int(gw)
        elif completed_gws:
            selected_gw = max(completed_gws)
        else:
            selected_gw = int(current_gw or 1)

        # Load manager histories once. Unlike build_prize_centre(), we do NOT
        # recalculate prizes for all 38 GWs and all MOTM periods.
        for row in standings:
            row["_current_gw"] = current_gw

        history_by_entry = await _load_prize_histories(standings)
        gw_rows = await _rank_gw_prizes(
            selected_gw,
            standings,
            history_by_entry,
        )

        # Correct GW summary statistics for the SELECTED GW, not just the live
        # standings. This makes historical GW selections accurate too.
        score_rows = []
        for row in standings:
            h = _history_by_gw(
                history_by_entry.get(row.get("entry"), {})
            ).get(selected_gw)

            if h is not None:
                score_rows.append(
                    (
                        int(h.get("points", 0) or 0),
                        row.get("player_name", "—"),
                    )
                )
            elif current_gw == selected_gw:
                score_rows.append(
                    (
                        int(row.get("event_total", 0) or 0),
                        row.get("player_name", "—"),
                    )
                )

        scores = [x[0] for x in score_rows]
        participants = len(standings)

        highest_score = max(scores) if scores else None
        lowest_score = min(scores) if scores else None
        average_score = (
            round(sum(scores) / len(scores), 1)
            if scores else None
        )

        highest_manager = next(
            (name for score, name in score_rows if score == highest_score),
            None,
        ) if highest_score is not None else None

        lowest_manager = next(
            (name for score, name in score_rows if score == lowest_score),
            None,
        ) if lowest_score is not None else None

        # LMS: use the persistent successful result first. Only rebuild LMS when
        # the cache does not contain the selected GW.
        lms_data = load_lms_cache()

        cached_has_selected_gw = bool(
            lms_data
            and any(
                int(r.get("gw", 0) or 0) == selected_gw
                for r in lms_data.get("rows", [])
            )
        )

        if not cached_has_selected_gw:
            try:
                fresh_lms = await build_lms_table(s)
                if fresh_lms.get("rows") or fresh_lms.get("survivors"):
                    save_lms_cache(fresh_lms)
                    lms_data = fresh_lms
            except Exception as lms_exc:
                # Keep the page usable even if the FPL API is temporarily slow.
                if not lms_data:
                    lms_data = {
                        "rows": [],
                        "survivors": [],
                        "started": False,
                        "message": (
                            "LMS data is temporarily unavailable. "
                            "Please refresh again in a moment."
                        ),
                    }

        lms_data = lms_data or {
            "rows": [],
            "survivors": [],
            "started": False,
            "message": None,
        }

        lms_row = next(
            (
                r for r in lms_data.get("rows", [])
                if int(r.get("gw", 0) or 0) == selected_gw
            ),
            None,
        )

        data = {
            "league_name": league_data.get(
                "league_name",
                s.get("league_name", "Achilles 8.0"),
            ),
            "current_gw": current_gw,
            "selected_gw": selected_gw,
            "next_gw": selected_gw + 1 if selected_gw < 38 else None,
            "available_gws": completed_gws,
            "gw_rows": gw_rows,
            "participants": participants,
            "highest_score": highest_score,
            "highest_manager": highest_manager,
            "lowest_score": lowest_score,
            "lowest_manager": lowest_manager,
            "average_score": average_score,
            "lms_row": lms_row,
            "lms_survivors": lms_data.get("survivors", []),
            "lms_started": lms_data.get("started", False),
            "lms_message": lms_data.get("message"),
        }

    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        "weekly_update.html",
        {
            "request": request,
            "s": s,
            "title": "Weekly Update",
            "error": error,
            **data,
        },
    )

@app.get("/competitions/{name}", response_class=HTMLResponse)
async def competition(request: Request, name: str):
    if name == "analytics":
        return await analytics(request, None)
    if name == "lms":
        return await lms(request)
    if name == "comparison":
        return await comparison(request, "", None)
    if name in {"european", "ucl", "europa", "conference"}:
        return await render_european(request, name)

    s = load_settings()
    labels = {"motm": "Manager of the Month", "prizes": "Prizes"}
    return templates.TemplateResponse(
        "competition.html",
        {"request": request, "s": s, "title": labels.get(name, name.title()), "name": name},
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "s": load_settings(),
            "saved": False,
        },
    )


@app.post("/admin", response_class=HTMLResponse)
async def admin_save(
    request: Request,
    league_id: str = Form(...),
    season: str = Form(...),
    expected_managers: int = Form(...),
    entry_fee: int = Form(...),
):
    s = load_settings()

    s["league_id"] = league_id.strip()
    s["season"] = season.strip()
    s["expected_managers"] = expected_managers
    s["entry_fee"] = entry_fee

    save_settings(s)
    _cache.clear()

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "s": s,
            "saved": True,
        },
    )
