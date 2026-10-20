"""Keeps data.json fresh for the Matthews vs McDavid page.

This script is run by GitHub every 10 minutes, but it only talks to the NHL when it is needed:
  * at the usual 3 AM and 3 PM Eastern refreshes,
  * on game nights, from 20 minutes before puck drop until 6 hours after it (so the page can show a live
    scoreboard and pick up final stats soon after the game),
  * when you press "Run workflow" by hand, or when there is no data.json yet.
Every other run exits straight away without touching anything.

What goes into data.json for each player:
  games : every regular-season game so far (date, opponent, goals, assists, points, ice time in seconds)
  sched : this team's games from 8 hours ago to a few days ahead (used for the "plays tonight" tag)
  live  : only while that player's game is on right now: period, clock, score, and his goals/assists tonight
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # very old Python, should not happen on GitHub
    ZoneInfo = None

PLAYERS = [
    {"id": "matthews", "nhl_id": 8479318, "name": "Auston Matthews", "team": "Toronto Maple Leafs", "abbr": "TOR", "num": 34, "pos": "C"},
    {"id": "mcdavid", "nhl_id": 8478402, "name": "Connor McDavid", "team": "Edmonton Oilers", "abbr": "EDM", "num": 97, "pos": "C"},
]
API = "https://api-web.nhle.com/v1/"
OUT = "data.json"
REFRESH_HOURS = (3, 15)          # Eastern time, 24-hour clock
BEFORE_PUCK_DROP = timedelta(minutes=20)
AFTER_PUCK_DROP = timedelta(hours=6)
LIVE_STATES = ("LIVE", "CRIT")
DONE_STATES = ("FINAL", "OFF")


def get_json(url, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "matthews-vs-mcdavid-tracker", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except Exception as err:  # try again a couple of times before giving up
            last = err
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("Could not fetch " + url + ": " + str(last))


def to_seconds(toi):
    # "22:14" -> 1334
    try:
        parts = [int(x) for x in str(toi or "0:00").split(":")]
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + (parts[1] if len(parts) > 1 else 0)


def parse_utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_games(p):
    log = get_json(API + "player/" + str(p["nhl_id"]) + "/game-log/now")
    # "now" can point at the playoffs once they start. The wager is regular season only (gameTypeId 2).
    if log.get("gameTypeId") != 2 and log.get("seasonId"):
        log = get_json(API + "player/" + str(p["nhl_id"]) + "/game-log/" + str(log["seasonId"]) + "/2")
    games = []
    for g in log.get("gameLog", []):
        games.append({
            "d": g.get("gameDate"),
            "o": g.get("opponentAbbrev"),
            "g": int(g.get("goals") or 0),
            "a": int(g.get("assists") or 0),
            "p": int(g.get("points") or 0),
            "t": to_seconds(g.get("toi")),
        })
    games.sort(key=lambda x: x["d"] or "")
    if not games:
        raise RuntimeError("No games returned for " + p["name"])
    return str(log.get("seasonId", "")), games


def load_schedule(p, now):
    """Regular-season games for this player's team from 8 hours ago onward (first few only)."""
    data = get_json(API + "club-schedule-season/" + p["abbr"] + "/now")
    out = []
    for g in data.get("games", []):
        if g.get("gameType") != 2 or not g.get("startTimeUTC"):
            continue
        start = parse_utc(g["startTimeUTC"])
        if start < now - timedelta(hours=8):
            continue
        home = (g.get("homeTeam") or {}).get("abbrev")
        away = (g.get("awayTeam") or {}).get("abbrev")
        is_home = home == p["abbr"]
        out.append({
            "id": g.get("id"),
            "s": g["startTimeUTC"],
            "st": g.get("gameState"),
            "o": away if is_home else home,
            "h": is_home,
        })
    out.sort(key=lambda x: x["s"])
    return out[:4]


def load_live(p, game):
    """Boxscore for a game that is on right now: score, period, clock and the player's night so far."""
    box = get_json(API + "gamecenter/" + str(game["id"]) + "/boxscore")
    home, away = box.get("homeTeam") or {}, box.get("awayTeam") or {}
    mine, theirs = (home, away) if game["h"] else (away, home)
    side = "homeTeam" if game["h"] else "awayTeam"
    goals = assists = 0
    stats = (box.get("playerByGameStats") or {}).get(side) or {}
    for group in ("forwards", "defense"):
        for row in stats.get(group, []):
            if row.get("playerId") == p["nhl_id"]:
                goals = int(row.get("goals") or 0)
                assists = int(row.get("assists") or 0)
    per = box.get("periodDescriptor") or {}
    clock = box.get("clock") or {}
    return {
        "id": game["id"],
        "st": box.get("gameState"),
        "o": game["o"],
        "h": game["h"],
        "per": per.get("number"),
        "pt": per.get("periodType"),
        "clk": clock.get("timeRemaining"),
        "intm": bool(clock.get("inIntermission")),
        "fs": int(mine.get("score") or 0),
        "as": int(theirs.get("score") or 0),
        "g": goals,
        "a": assists,
    }


def eastern_hour(now):
    if ZoneInfo is None:
        return (now.hour - 4) % 24
    return now.astimezone(ZoneInfo("America/New_York")).hour


def why_refresh(existing, now, force):
    """Returns a short reason to refresh now, or None if there is nothing to do."""
    if force:
        return "run by hand"
    if not existing or not existing.get("updatedAt"):
        return "no data yet"
    try:
        age = now - parse_utc(existing["updatedAt"])
    except ValueError:
        return "unreadable timestamp"
    if age > timedelta(hours=14):
        return "data is more than 14 hours old"
    if eastern_hour(now) in REFRESH_HOURS and age > timedelta(hours=3):
        return "regular 3 AM / 3 PM refresh"
    for pl in existing.get("players", []):
        if pl.get("live"):
            return "a game is live"
        for g in pl.get("sched", []):
            try:
                start = parse_utc(g["s"])
            except (KeyError, ValueError):
                continue
            if start - BEFORE_PUCK_DROP <= now <= start + AFTER_PUCK_DROP:
                return "game night window"
    return None


def build(now):
    games_by_player = []
    season = ""
    for p in PLAYERS:
        season, games = load_games(p)
        games_by_player.append(games)
    players = []
    for p, games in zip(PLAYERS, games_by_player):
        entry = {k: p[k] for k in ("id", "name", "team", "abbr", "num", "pos")}
        entry["games"] = games
        try:
            sched = load_schedule(p, now)
        except Exception as err:  # the schedule is a bonus, never block the stats on it
            print("Schedule problem for " + p["name"] + ": " + str(err), file=sys.stderr)
            sched = None
        entry["_sched"] = sched
        players.append(entry)
    return season, players


def main(now=None, force=None, existing=None):
    now = now or datetime.now(timezone.utc)
    if force is None:
        force = bool(os.environ.get("FORCE"))
    if existing is None and os.path.exists(OUT):
        try:
            with open(OUT) as f:
                existing = json.load(f)
        except Exception:
            existing = None
    reason = why_refresh(existing, now, force)
    if reason is None:
        print("Nothing to do right now.")
        return False
    print("Refreshing: " + reason)

    season, players = build(now)
    old = {pl["id"]: pl for pl in (existing or {}).get("players", [])}
    for pl, p in zip(players, PLAYERS):
        sched = pl.pop("_sched")
        if sched is None:  # keep what we had if the schedule was unreachable
            sched = old.get(pl["id"], {}).get("sched", [])
        pl["sched"] = sched
        live_game = next((g for g in sched if g.get("st") in LIVE_STATES), None)
        if live_game:
            try:
                pl["live"] = load_live(p, live_game)
            except Exception as err:
                print("Live score problem for " + p["name"] + ": " + str(err), file=sys.stderr)

    data = {
        "season": season,
        "updatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "players": players,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, OUT)  # only replaced when everything above worked, so old numbers survive a failure
    print("Saved " + OUT + ": " + ", ".join(
        pl["name"] + " " + str(len(pl["games"])) + " games" + (" (LIVE)" if pl.get("live") else "") for pl in players))
    return True


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print("ERROR: " + str(err), file=sys.stderr)
        sys.exit(1)
