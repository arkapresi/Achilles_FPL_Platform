from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

# This module is deliberately self-contained so the existing League,
# Manager, Analytics, Comparison and LMS pages are not changed.

GROUPS_FILE = Path(__file__).parent / "data" / "european_groups.json"
GROUP_NAMES = list("ABCDEFGH")
FIRST_LEG_5 = [10, 11, 12, 13, 14]
SECOND_LEG_5 = [16, 17, 18, 19, 20]
# Current 2026/27 Achilles European template: 8 groups x 5 managers.
# Keep the 4-member constants for backward compatibility if the format changes later.
FIRST_LEG_4 = [10, 11, 12]
SECOND_LEG_4 = [16, 17, 18]


def load_groups() -> dict[str, list[str]]:
    if not GROUPS_FILE.exists():
        return {g: [] for g in GROUP_NAMES}
    try:
        data = json.loads(GROUPS_FILE.read_text())
    except Exception:
        return {g: [] for g in GROUP_NAMES}
    return {g: list(data.get(g, []))[:5] for g in GROUP_NAMES}


def _round_robin(items: list[str]) -> list[list[tuple[str, str] | None]]:
    """Circle-method round robin. For odd N, one bye is inserted."""
    teams = list(items)
    if len(teams) < 2:
        return []
    if len(teams) % 2:
        teams.append("__BYE__")
    n = len(teams)
    rounds = []
    arr = teams[:]
    for _ in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = arr[i], arr[-1 - i]
            pairs.append(None if "__BYE__" in (a, b) else (a, b))
        rounds.append(pairs)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    return rounds


def _schedule_for_group(members: list[str]) -> list[dict[str, Any]]:
    n = len(members)
    if n not in (4, 5):
        return []
    rounds = _round_robin(members)
    if n == 5:
        gws = FIRST_LEG_5 + SECOND_LEG_5
    else:
        gws = FIRST_LEG_4 + SECOND_LEG_4
    fixtures = []
    for leg, leg_rounds in enumerate((rounds, rounds), start=1):
        leg_gws = FIRST_LEG_5 if n == 5 and leg == 1 else SECOND_LEG_5 if n == 5 else FIRST_LEG_4 if leg == 1 else SECOND_LEG_4
        for rno, pairs in enumerate(leg_rounds):
            gw = leg_gws[rno]
            for pair_no, pair in enumerate(pairs, start=1):
                if not pair:
                    continue
                # Reverse home/away in the second leg.
                home, away = pair if leg == 1 else (pair[1], pair[0])
                fixtures.append({
                    "group": None,
                    "leg": leg,
                    "round": rno + 1,
                    "gw": gw,
                    "home": home,
                    "away": away,
                })
    return fixtures


def build_schedule(groups: dict[str, list[str]]) -> list[dict[str, Any]]:
    rows = []
    for group, members in groups.items():
        for row in _schedule_for_group(members):
            row["group"] = group
            rows.append(row)
    return sorted(rows, key=lambda x: (x["gw"], x["group"], x["round"], x["home"]))


async def build_history_map(groups: dict[str, list[str]], fpl_get):
    names = []
    for members in groups.values():
        names.extend(members)
    names = list(dict.fromkeys(names))

    # We need entry IDs for managers. The caller supplies standings from the
    # configured Achilles league. Matching is performed outside this helper.
    return names


def _net(h: dict[str, Any] | None) -> int | None:
    if not h:
        return None
    points = h.get("points")
    if points is None:
        return None
    return int(points or 0) - int(h.get("event_transfers_cost", 0) or 0)


def _metric(h: dict[str, Any] | None, key: str, default: int = 0) -> int:
    try:
        return int((h or {}).get(key, default) or default)
    except (TypeError, ValueError):
        return default


async def get_histories(groups: dict[str, list[str]], standings: list[dict[str, Any]], fpl_get):
    by_name = {(r.get("player_name") or "").strip().lower(): r for r in standings}
    selected = {}
    for group, members in groups.items():
        for name in members:
            row = by_name.get(name.strip().lower())
            if row:
                selected[row.get("entry")] = {"group": group, "name": name, "row": row}

    async def one(entry_id: int, meta: dict[str, Any]):
        try:
            hist = await fpl_get(f"entry/{entry_id}/history/", 300)
            return entry_id, meta, {int(h.get("event")): h for h in hist.get("current", []) if h.get("event")}
        except Exception:
            return entry_id, meta, {}

    results = await asyncio.gather(*(one(e, m) for e, m in selected.items()))
    out = {e: {"meta": m, "history": h, "metrics": {}} for e, m, h in results}

    # Player-event metrics are fetched only for the European group-stage GWs.
    # live/{gw} is shared across all managers; picks are manager-specific.
    available_events = set()
    for obj in out.values():
        available_events.update(obj.get("history", {}).keys())
    gws = sorted(available_events.intersection(set(FIRST_LEG_5 + SECOND_LEG_5)))
    live_by_gw = {}
    for gw in gws:
        try:
            live = await fpl_get(f"event/{gw}/live/", 300)
            live_by_gw[gw] = {x.get("id"): x.get("stats", {}) for x in live.get("elements", [])}
        except Exception:
            live_by_gw[gw] = {}

    async def metrics_for(entry_id: int, gw: int):
        try:
            picks_data = await fpl_get(f"entry/{entry_id}/event/{gw}/picks/", 300)
        except Exception:
            return None
        picks = picks_data.get("picks", [])
        live_map = live_by_gw.get(gw, {})
        starters = [p for p in picks if 1 <= int(p.get("position") or 99) <= 11]
        totals = {"goals_scored": 0, "assists": 0, "clean_sheets": 0,
                  "goals_conceded": 0, "bonus": 0, "yellow_cards": 0,
                  "red_cards": 0, "captain_points": 0, "vice_captain_points": 0}
        for p in starters:
            st = live_map.get(p.get("element"), {})
            for key in ("goals_scored", "assists", "clean_sheets", "goals_conceded", "bonus", "yellow_cards", "red_cards"):
                totals[key] += _metric(st, key)
            pts = _metric(st, "total_points")
            mult = int(p.get("multiplier", 0) or 0)
            if p.get("is_captain"):
                totals["captain_points"] = pts * mult
            if p.get("is_vice_captain"):
                totals["vice_captain_points"] = pts * mult
        return totals

    tasks = []
    keys = []
    for entry_id in out:
        for gw in gws:
            tasks.append(metrics_for(entry_id, gw))
            keys.append((entry_id, gw))
    results2 = await asyncio.gather(*tasks, return_exceptions=True)
    for (entry_id, gw), result in zip(keys, results2):
        if isinstance(result, Exception) or result is None:
            continue
        out[entry_id]["metrics"][gw] = result
    return out


def _sort_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Rulebook group-stage tie-break hierarchy:
    # 1 Point Difference, 2 goals, 3 assists, 4 lower goals conceded,
    # 5 bonus, 6 fewer yellow, 7 fewer red, 8 captain points, 9 lower transfer cost.
    return sorted(rows, key=lambda x: (
        -x["points"],
        -x["diff"],
        -x["goals"],
        -x["assists"],
        x["goals_conceded"],
        -x["bonus"],
        x["yellow"],
        x["red"],
        -x["captain_points"],
        x["transfer_cost"],
        x["manager"].lower(),
    ))


def build_group_tables(groups: dict[str, list[str]], histories: dict[int, dict[str, Any]], standings: list[dict[str, Any]], schedule: list[dict[str, Any]]):
    name_to_entry = {}
    for entry_id, obj in histories.items():
        name_to_entry[obj["meta"]["name"].strip().lower()] = entry_id

    tables = {}
    fixture_rows = []
    for g in GROUP_NAMES:
        members = groups.get(g, [])
        if len(members) not in (4, 5):
            tables[g] = []
            continue
        acc = {}
        for name in members:
            acc[name] = {
                "manager": name, "played": 0, "won": 0, "tied": 0, "lost": 0,
                "scored_for": 0, "scored_against": 0, "points": 0,
                "diff": 0, "goals": 0, "assists": 0, "goals_conceded": 0,
                "bonus": 0, "yellow": 0, "red": 0, "captain_points": 0,
                "transfer_cost": 0,
            }
        for fx in [x for x in schedule if x["group"] == g]:
            h = fx["home"].strip().lower(); a = fx["away"].strip().lower()
            he = name_to_entry.get(h); ae = name_to_entry.get(a)
            hh = histories.get(he, {}).get("history", {}).get(fx["gw"]) if he else None
            ah = histories.get(ae, {}).get("history", {}).get(fx["gw"]) if ae else None
            hs = _net(hh); aas = _net(ah)
            ready = hs is not None and aas is not None
            fixture_rows.append({**fx, "home_score": hs, "away_score": aas, "ready": ready})
            if not ready:
                continue
            H, A = acc[fx["home"]], acc[fx["away"]]
            H["played"] += 1; A["played"] += 1
            H["scored_for"] += hs; H["scored_against"] += aas
            A["scored_for"] += aas; A["scored_against"] += hs
            if hs > aas:
                H["won"] += 1; A["lost"] += 1; H["points"] += 3
            elif hs < aas:
                A["won"] += 1; H["lost"] += 1; A["points"] += 3
            else:
                H["tied"] += 1; A["tied"] += 1
            for obj, entry_id, hist in ((H, he, hh), (A, ae, ah)):
                metrics = histories.get(entry_id, {}).get("metrics", {}).get(fx["gw"], {}) if entry_id else {}
                obj["goals"] += _metric(metrics, "goals_scored")
                obj["assists"] += _metric(metrics, "assists")
                obj["goals_conceded"] += _metric(metrics, "goals_conceded")
                obj["bonus"] += _metric(metrics, "bonus")
                obj["yellow"] += _metric(metrics, "yellow_cards")
                obj["red"] += _metric(metrics, "red_cards")
                obj["captain_points"] += _metric(metrics, "captain_points")
                obj["transfer_cost"] += _metric(hist, "event_transfers_cost")
        for row in acc.values():
            row["diff"] = row["scored_for"] - row["scored_against"]
        tables[g] = _sort_table(list(acc.values()))
    return tables, fixture_rows


def qualification_rows(tables: dict[str, list[dict[str, Any]]]):
    rows = []
    for g, table in tables.items():
        for pos, r in enumerate(table, 1):
            # Current Achilles European template: every group has 5 managers.
            # 1st/2nd -> UCL, 3rd/4th -> Europa, 5th -> Conference.
            # This can be changed later without affecting the group-stage logic.
            if pos <= 2:
                dest = "UCL"
            elif pos <= 4:
                dest = "Europa"
            elif pos == 5:
                dest = "Conference"
            else:
                dest = "Eliminated"
            rows.append({"group": g, "position": pos, "manager": r["manager"], "points": r["points"], "diff": r["diff"], "destination": dest})
    return rows
