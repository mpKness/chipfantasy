import argparse
import json
import os
import webbrowser
import csv
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests
from requests_oauthlib import OAuth2Session

# ========= CONFIG (PLEASE MOVE SECRETS TO ENV VARS) =========
# RECOMMENDED:
# export YAHOO_CLIENT_ID="..."
# export YAHOO_CLIENT_SECRET="..."
# export YAHOO_LEAGUE_KEY="461.l.418123"

CLIENT_ID = os.getenv("YAHOO_CLIENT_ID", "YOUR_CLIENT_ID")
CLIENT_SECRET = os.getenv("YAHOO_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
REDIRECT_URI = os.getenv("YAHOO_REDIRECT_URI", "https://localhost/callback")
TOKEN_FILE = os.getenv("YAHOO_TOKEN_FILE", "yahoo_token.json")

# Use the numeric league key from your JSON
# 461.l.418123 is fantasy football 2025
# 449.l.979792 is fantasy football 2024
LEAGUE_KEY = os.getenv("YAHOO_LEAGUE_KEY", "449.l.418123")
# ===========================================================

AUTH_BASE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
FANTASY_ENDPOINT = "https://fantasysports.yahooapis.com/fantasy/v2"


def save_token(token: dict):
    with open(TOKEN_FILE, "w") as f:
        json.dump(token, f, indent=2)


def load_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, "r") as f:
        return json.load(f)


def get_oauth_session(token=None, auto_refresh=True) -> OAuth2Session:
    extra = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}

    if token and auto_refresh:
        return OAuth2Session(
            CLIENT_ID,
            token=token,
            auto_refresh_url=TOKEN_URL,
            auto_refresh_kwargs=extra,
            token_updater=save_token,
            redirect_uri=REDIRECT_URI,
        )
    return OAuth2Session(CLIENT_ID, redirect_uri=REDIRECT_URI)


def do_initial_auth_flow():
    oauth = get_oauth_session()
    auth_url, _ = oauth.authorization_url(AUTH_BASE_URL)

    print("Open this URL in your browser and authorize the app:")
    print(auth_url)
    print()

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    redirect_response = input(
        "After approving, paste the FULL redirect URL from your browser here:\n> "
    ).strip()

    parsed = urlparse(redirect_response)
    code = parse_qs(parsed.query).get("code")
    if not code:
        raise RuntimeError("Could not find 'code' parameter in the URL you pasted.")
    code = code[0]

    token = oauth.fetch_token(
        TOKEN_URL,
        code=code,
        client_secret=CLIENT_SECRET,
        include_client_id=True,
    )
    save_token(token)
    print("Token saved to", TOKEN_FILE)
    return token


def get_token():
    token = load_token()
    if token:
        return token
    return do_initial_auth_flow()


def yahoo_get(path: str, params=None):
    token = get_token()
    oauth = get_oauth_session(token=token)

    url = f"{FANTASY_ENDPOINT}{path}"
    params = params or {}
    params["format"] = "json"  # force JSON

    resp = oauth.get(url, params=params)

    if not resp.ok:
        print("Request failed:", resp.status_code)
        print("Content-Type:", resp.headers.get("Content-Type"))
        print("Body (first 500 chars):")
        print(resp.text[:500])
        resp.raise_for_status()

    return resp.json()


def get_league_scoreboard(league_key: str, week: int):
    path = f"/league/{league_key}/scoreboard"
    return yahoo_get(path, params={"week": str(week)})


# ---------- Matchup parsing (your working exact-path version) ----------

def scoreboard_to_rows(scoreboard_json, week: int):
    rows = []

    league_list = scoreboard_json["fantasy_content"]["league"]
    scoreboard = league_list[1]["scoreboard"]
    scoreboard_block = scoreboard["0"]
    matchups = scoreboard_block["matchups"]

    for matchup_index, matchup_entry in matchups.items():
        if not matchup_index.isdigit():
            continue

        matchup = matchup_entry["matchup"]
        teams_container = matchup["0"]["teams"]

        teams = []
        for team_index, team_entry in teams_container.items():
            if team_index.isdigit():
                teams.append(team_entry["team"])

        if len(teams) != 2:
            continue

        t1, t2 = teams

        t1_meta = t1[0]
        t1_score = t1[1]
        team1_name = next(x["name"] for x in t1_meta if isinstance(x, dict) and "name" in x)
        team1_points = float(t1_score["team_points"]["total"])

        t2_meta = t2[0]
        t2_score = t2[1]
        team2_name = next(x["name"] for x in t2_meta if isinstance(x, dict) and "name" in x)
        team2_points = float(t2_score["team_points"]["total"])

        if team1_points > team2_points:
            winner = team1_name
        elif team2_points > team1_points:
            winner = team2_name
        else:
            winner = "TIE"

        rows.append(
            {
                "week": week,
                "matchup_index": int(matchup_index) + 1,
                "team1": team1_name,
                "team1_points": team1_points,
                "team2": team2_name,
                "team2_points": team2_points,
                "winner": winner,
            }
        )

    return rows


def write_matchups_csv(rows, out_path: str):
    fieldnames = ["week", "matchup_index", "team1", "team1_points", "team2", "team2_points", "winner"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ---------- NEW: Player export (roster + weekly player_points) ----------

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _iter_numeric_dict(d):
    if not isinstance(d, dict):
        return
    for k in sorted([k for k in d.keys() if str(k).isdigit()], key=lambda s: int(s)):
        yield d[k]


def _coerce_float(x):
    try:
        return float(x)
    except Exception:
        return None


def extract_team_key_from_team(team_obj):
    # team_obj[0] is list of dicts that includes {"team_key": "..."}
    meta = team_obj[0]
    for item in meta:
        if isinstance(item, dict) and "team_key" in item:
            return item["team_key"]
    return None


def get_matchups_with_team_context(scoreboard_json, week: int):
    """
    Returns list of:
      {matchup_index, team1_name, team1_key, team2_name, team2_key}
    """
    league_list = scoreboard_json["fantasy_content"]["league"]
    scoreboard = league_list[1]["scoreboard"]
    matchups = scoreboard["0"]["matchups"]

    out = []
    for matchup_index, matchup_entry in matchups.items():
        if not matchup_index.isdigit():
            continue

        matchup = matchup_entry["matchup"]
        teams_container = matchup["0"]["teams"]

        teams = []
        for team_index, team_entry in teams_container.items():
            if team_index.isdigit():
                teams.append(team_entry["team"])

        if len(teams) != 2:
            continue

        t1, t2 = teams
        t1_name = next(x["name"] for x in t1[0] if isinstance(x, dict) and "name" in x)
        t2_name = next(x["name"] for x in t2[0] if isinstance(x, dict) and "name" in x)
        t1_key = extract_team_key_from_team(t1)
        t2_key = extract_team_key_from_team(t2)

        out.append({
            "week": week,
            "matchup_index": int(matchup_index) + 1,
            "team1_name": t1_name,
            "team1_key": t1_key,
            "team2_name": t2_name,
            "team2_key": t2_key,
        })

    return out


def get_team_roster_with_weekly_points(team_key: str, week: int):
    """
    This is the critical endpoint:
      roster for that week + players + weekly stats/points
    """
    path = f"/team/{team_key}/roster;week={week}/players/stats;type=week;week={week}"
    return yahoo_get(path, params={})


def _pick_best_players_container(json_obj):
    """
    Find the largest 'players' container (by count) anywhere in the response.
    Yahoo nesting varies, so we choose the one with the biggest count.
    """
    candidates = []
    for n in _walk(json_obj):
        if isinstance(n, dict) and "players" in n and isinstance(n["players"], dict):
            candidates.append(n["players"])
    if not candidates:
        return None

    def score(players_dict):
        c = players_dict.get("count")
        try:
            return int(c)
        except Exception:
            return 0

    return max(candidates, key=score)


def parse_team_players_from_roster_stats(roster_stats_json, week: int, team_name: str, team_key: str):
    """
    Returns rows:
      player_name, player_key, roster_slot, is_starter, points
    Handles the Yahoo shape you saw:
      player: [ [meta dicts...], {selected_position:[...]} ]  (roster-only)
    AND the roster+stats shape:
      player: [ [meta dicts...], {player_points:{total:...}, ...}, {selected_position:[...]} ] (sometimes)
    We'll search within each player node for selected_position and player_points.
    """
    players_container = _pick_best_players_container(roster_stats_json)
    if not players_container:
        return []

    rows = []
    for entry in _iter_numeric_dict(players_container):
        if not isinstance(entry, dict):
            continue
        player = entry.get("player")
        if not isinstance(player, list) or len(player) == 0:
            continue

        # meta is often player[0] as list of dicts
        meta_list = player[0] if isinstance(player[0], list) else []

        player_key = ""
        player_name = ""
        primary_pos = ""
        nfl_team = ""

        for d in meta_list:
            if not isinstance(d, dict):
                continue
            if "player_key" in d:
                player_key = d["player_key"]
            if "name" in d and isinstance(d["name"], dict):
                player_name = d["name"].get("full", player_name)
            if "primary_position" in d:
                primary_pos = d.get("primary_position") or primary_pos
            if "editorial_team_abbr" in d:
                nfl_team = d.get("editorial_team_abbr") or nfl_team

        # Find roster slot anywhere in player structure
        roster_slot = ""
        for n in _walk(player):
            if isinstance(n, dict) and "selected_position" in n:
                sp = n["selected_position"]
                if isinstance(sp, list):
                    for x in sp:
                        if isinstance(x, dict) and "position" in x:
                            roster_slot = x["position"]
                            break
            if roster_slot:
                break

        # Find weekly points anywhere in player structure
        points = None
        for n in _walk(player):
            if isinstance(n, dict) and "player_points" in n and isinstance(n["player_points"], dict):
                points = _coerce_float(n["player_points"].get("total"))
                break

        if not player_key:
            continue

        is_starter = roster_slot not in ("BN", "IR", "IL", "")

        rows.append({
            "week": week,
            "team_name": team_name,
            "team_key": team_key,
            "player_name": player_name,
            "player_key": player_key,
            "nfl_team": nfl_team,
            "primary_pos": primary_pos,
            "roster_slot": roster_slot,
            "is_starter": 1 if is_starter else 0,
            "points": points,
        })

    return rows

def get_nfl_game_keys():
    path = "/users;use_login=1/games;game_codes=nfl"
    data = yahoo_get(path, params={})
    return data

def get_my_nfl_leagues():
    # Shows leagues you're in; includes league_key + name + season/game_key
    path = "/users;use_login=1/games;game_codes=nfl/leagues"
    return yahoo_get(path, params={})

def write_players_csv(rows, out_path: str):
    fieldnames = [
        "week",
        "matchup_index",
        "team_name",
        "team_key",
        "opponent_name",
        "player_name",
        "player_key",
        "nfl_team",
        "primary_pos",
        "roster_slot",
        "is_starter",
        "points",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fieldnames})


def main():
    ap = argparse.ArgumentParser(description="Export Yahoo Fantasy Football weekly matchup + player performance")
    ap.add_argument("--week", type=int, required=True, help="Week number to export (e.g. 1)")
    ap.add_argument("--league-key", type=str, default=LEAGUE_KEY, help="Yahoo league key (default from env/config)")
    ap.add_argument("--matchups-out", type=str, default=None, help="Matchups CSV path")
    ap.add_argument("--players-out", type=str, default=None, help="Players CSV path")
    ap.add_argument("--dump", action="store_true", help="Dump raw league scoreboard JSON")
    args = ap.parse_args()

    matchups_out = args.matchups_out or f"yahoo_week_{args.week}.csv"
    players_out = args.players_out or f"yahoo_week_{args.week}_players.csv"

    data = get_league_scoreboard(args.league_key, args.week)

    if args.dump:
        with open(f"raw_week_{args.week}.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"Wrote raw_week_{args.week}.json")

    # 1) Matchup summary CSV
    matchup_rows = scoreboard_to_rows(data, args.week)
    write_matchups_csv(matchup_rows, matchups_out)
    print(f"Wrote {len(matchup_rows)} rows to {matchups_out}")

    # 2) Player performance CSV
    matchups = get_matchups_with_team_context(data, args.week)

    all_player_rows = []
    for m in matchups:
        for team_name, team_key, opp_name in [
            (m["team1_name"], m["team1_key"], m["team2_name"]),
            (m["team2_name"], m["team2_key"], m["team1_name"]),
        ]:
            roster_stats_json = get_team_roster_with_weekly_points(team_key, args.week)

            # Optional: dump one team roster+stats to inspect
            # (uncomment if you need it)
            # fname = f"raw_roster_stats_{team_key.replace('.', '_')}_week_{args.week}.json"
            # if not os.path.exists(fname):
            #     with open(fname, "w", encoding="utf-8") as f:
            #         json.dump(roster_stats_json, f, indent=2)
            #     print("Wrote", fname)

            team_player_rows = parse_team_players_from_roster_stats(
                roster_stats_json, args.week, team_name=team_name, team_key=team_key
            )

            # attach matchup context
            for r in team_player_rows:
                r["matchup_index"] = m["matchup_index"]
                r["opponent_name"] = opp_name
                all_player_rows.append(r)

            print(f"  Team '{team_name}' ({team_key}): {len(team_player_rows)} players")

    # Sort: group by team, starters first, then roster slot, then name
    all_player_rows.sort(key=lambda r: (r["team_name"], -r["is_starter"], r["roster_slot"], r["player_name"]))

    write_players_csv(all_player_rows, players_out)
    print(f"Wrote {len(all_player_rows)} rows to {players_out}")

    print("Export time:", datetime.now(timezone.utc).isoformat())

    #data = get_nfl_game_keys()
    #with open("debug_nfl_games.json", "w") as f:
    #    json.dump(data, f, indent=2)

    #data = get_my_nfl_leagues()
    #with open("my_nfl_leagues.json", "w", encoding="utf-8") as f:
    #    json.dump(data, f, indent=2)
    #print("Wrote my_nfl_leagues.json")

if __name__ == "__main__":
    main()
