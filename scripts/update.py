#!/usr/bin/env python3
"""Football predictor. Fetch results -> fit own model -> predictions.json + public track record.
Usage:  python scripts/update.py          (needs env FOOTBALL_DATA_KEY)
        python scripts/update.py --demo   (synthetic data, to test offline)
Uses only the Python standard library. No bookmaker odds or outside predictions are used."""
import os, sys, json, math, time, random, re, unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.parse import quote

COMPS = {"PL": "Premier League", "PD": "La Liga", "SA": "Serie A", "BL1": "Bundesliga", "FL1": "Ligue 1", "ELC": "Championship", "DED": "Eredivisie", "PPL": "Primeira Liga", "BSA": "Brazil Serie A", "CL": "Champions League", "EC": "European Championship", "WC": "World Cup"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "data")
XI = 0.006          # time decay per day: recent games count more
K = 3.0             # shrinkage: pulls small samples toward league average
TRACK_DAYS = 4      # lock track-record picks only this close to kickoff
BETSLIP_DAYS = 7    # keep betslips focused on the next week
MIN_MATCHES = 20    # skip a league until enough games were played
NM = {"1": "Home win", "X": "Draw", "2": "Away win"}
PITCH_COMPS = {"Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1"}
PITCH_BASE = "https://api.pitchapi.dev/v1"
STAT_MATCHES_PER_TEAM = 5

def parse(s): return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
def r3(a, b): return "1" if a > b else "X" if a == b else "2"
def pc(x): return f"{x*100:.0f}%"
def pmf(l, n):
    a = [math.exp(-l)]
    for k in range(1, n + 1): a.append(a[-1] * l / k)
    return a

# ---------- data ----------
def fetch(code, key):
    req = Request(f"https://api.football-data.org/v4/competitions/{code}/matches", headers={"X-Auth-Token": key})
    with urlopen(req, timeout=30) as r: return json.load(r)["matches"]

def norm(m):
    s = m["score"]; ft = s.get("fullTime") or {}; ht = s.get("halfTime") or {}
    nm = lambda t: t.get("shortName") or t["name"]
    return dict(id=m["id"], date=parse(m["utcDate"]), status=m["status"], home=nm(m["homeTeam"]), away=nm(m["awayTeam"]),
                ft=(ft["home"], ft["away"]) if ft.get("home") is not None else None,
                ht=(ht["home"], ht["away"]) if ht.get("home") is not None else None)

def pitch_json(path, key):
    req = Request(PITCH_BASE + path, headers={"X-API-KEY": key, "Accept": "application/json"})
    with urlopen(req, timeout=30) as response: return json.load(response)

def team_key(name):
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    aliases = {"man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
               "ath madrid": "atletico madrid", "atletico": "atletico madrid", "ath bilbao": "athletic club",
               "inter": "internazionale", "ac milan": "milan", "paris sg": "paris saint germain", "psg": "paris saint germain",
               "spurs": "tottenham hotspur", "wolves": "wolverhampton wanderers", "nott m forest": "nottingham forest"}
    return aliases.get(text, text)

def pitch_date(match):
    value = match.get("time_utc") or match.get("date")
    if not value: return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError): return None

def number(value):
    if isinstance(value, dict): value = value.get("value")
    if value is None: return None
    found = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(found.group(0)) if found else None

def player_fields(row):
    fields = {}
    for group in row.get("stats", []) or []:
        for label, item in (group.get("stats", {}) or {}).items():
            stat = item.get("stat", item) if isinstance(item, dict) else item
            key = (item.get("key") if isinstance(item, dict) else None) or (stat.get("key") if isinstance(stat, dict) else None)
            val = number(stat)
            if key: fields[str(key).lower()] = val
            fields[re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")] = val
    return dict(id=str((row.get("player") or {}).get("id", "")), name=(row.get("player") or {}).get("name", "Unknown player"),
                minutes=fields.get("minutes_played", fields.get("minutes", 0)) or 0,
                goals=fields.get("goals", 0) or 0, assists=fields.get("assists", 0) or 0,
                xg=fields.get("expected_goals", fields.get("xg")), xa=fields.get("expected_assists", fields.get("xa")),
                xg_xa=fields.get("xg_and_xa"))

def match_stat_pairs(data):
    periods = data.get("periods", []) if isinstance(data, dict) else []
    all_period = next((p for p in periods if str(p.get("period", "")).lower() in ("all", "fulltime", "full_time")), None)
    groups = (all_period or {}).get("groups", [])
    if not groups:
        groups = [g for p in periods for g in p.get("groups", [])]
    pairs = []
    for group in groups:
        for item in group.get("items", []) or []:
            label = " ".join(str(item.get(k, "")) for k in ("title", "key", "name")).lower()
            home, away = number(item.get("home")), number(item.get("away"))
            if home is not None and away is not None: pairs.append((label, home, away))
    return pairs

def extract_match_stats(data):
    pairs = match_stat_pairs(data)
    corner = next(((h, a) for label, h, a in pairs if "corner" in label and not any(x in label for x in ("first half", "second half", "1h", "2h"))), None)
    yellow = next(((h, a) for label, h, a in pairs if "yellow" in label and "card" in label), None)
    red = next(((h, a) for label, h, a in pairs if "red" in label and "card" in label), None)
    total = next(((h, a) for label, h, a in pairs if "card" in label and "yellow" not in label and "red" not in label), None)
    if yellow or red:
        cards = ((yellow[0] if yellow else 0) + (red[0] if red else 0), (yellow[1] if yellow else 0) + (red[1] if red else 0))
    else: cards = total
    return corner, cards

def fetch_pitch_data(now, key):
    """Optional detailed feed for player and match-stat projections; failures never stop core predictions."""
    raw = pitch_json("/leagues", key)
    leagues = (raw.get("data") or {}).get("leagues", [])
    start = now.year if now.month >= 7 else now.year - 1
    season = f"{start}/{start + 1}"
    by_name = {team_key(x.get("name", "")): x for x in leagues}
    details, schedules, notes = {}, {}, []
    for comp in sorted(PITCH_COMPS):
        league = by_name.get(team_key(comp))
        if not league:
            notes.append(f"{comp}: no detailed-stat league match found"); continue
        try:
            answer = pitch_json(f"/leagues/{league['id']}/matches?season={quote(season)}&status=all", key)
            rows = (answer.get("data") or {}).get("matches", [])
            schedules[comp] = rows
            played = [m for m in rows if str(m.get("status", "")).lower() in ("finished", "complete", "completed") and m.get("score_home") is not None and m.get("score_away") is not None]
            # Recent matches from each team give current-season form without stale player transfers.
            selected = {}
            teams = {team_key((m.get(side) or {}).get("name", "")) for m in played for side in ("home_team", "away_team")}
            for team in teams:
                team_rows = [m for m in played if team in (team_key((m.get("home_team") or {}).get("name", "")), team_key((m.get("away_team") or {}).get("name", "")))]
                for m in sorted(team_rows, key=lambda x: pitch_date(x) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:STAT_MATCHES_PER_TEAM]:
                    if m.get("id"): selected[m["id"]] = m
            def details_for(match):
                mid = quote(str(match["id"]), safe="")
                players = pitch_json(f"/matches/{mid}/players", key).get("data", [])
                stats = pitch_json(f"/matches/{mid}/stats", key).get("data", {})
                corners, cards = extract_match_stats(stats)
                p_rows = [player_fields(x) | {"team_id": x.get("team_id")} for x in players if (x.get("player") or {}).get("id")]
                return match, dict(players=p_rows, corners=corners, cards=cards)
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(details_for, m) for m in selected.values()]
                for future in as_completed(futures):
                    try:
                        match, stats = future.result(); details[str(match["id"])] = (comp, match, stats)
                    except Exception:
                        # Individual stat records can be missing; keep the rest of the league usable.
                        continue
        except Exception as error:
            notes.append(f"{comp}: detailed stats unavailable ({type(error).__name__})")
    return dict(details=details, schedules=schedules, notes=notes)

def pitch_match_for(match, comp, schedules):
    home, away = team_key(match["home"]), team_key(match["away"])
    choices = []
    for candidate in schedules.get(comp, []):
        ch = team_key((candidate.get("home_team") or {}).get("name", "")); ca = team_key((candidate.get("away_team") or {}).get("name", ""))
        if home != ch or away != ca: continue
        dt = pitch_date(candidate)
        if not dt: continue
        delta = abs((dt - match["date"]).total_seconds())
        if delta <= 36 * 3600: choices.append((delta, candidate))
    return min(choices, key=lambda x: x[0])[1] if choices else None

def player_projections(team, team_id, expected_goals, details):
    by_player = {}
    for comp, match, detail in details.values():
        dt = pitch_date(match)
        if not dt: continue
        home = (match.get("home_team") or {}); away = (match.get("away_team") or {})
        for row in detail.get("players", []):
            belongs = row.get("team_id") == team_id if team_id else team_key(home.get("name") if row.get("team_id") == home.get("id") else away.get("name", "")) == team_key(team)
            if not belongs or row.get("minutes", 0) <= 0: continue
            obj = by_player.setdefault(row["id"], dict(name=row["name"], minutes=[], goals=0.0, assists=0.0, xg=0.0, xa=0.0, has_xg=False, matches=0, latest=dt))
            obj["minutes"].append((dt, float(row["minutes"])))
            obj["goals"] += float(row.get("goals") or 0); obj["assists"] += float(row.get("assists") or 0); obj["matches"] += 1
            if row.get("xg") is not None:
                obj["xg"] += float(row["xg"]); obj["has_xg"] = True
                if row.get("xg_xa") is not None: obj["xa"] += max(0.0, float(row["xg_xa"]) - float(row["xg"]))
            elif row.get("xa") is not None: obj["xa"] += float(row["xa"])
            if dt > obj["latest"]: obj["latest"] = dt
    score, assist = [], []
    for pid, p in by_player.items():
        if p["matches"] < 2: continue
        minutes_total = sum(v for _, v in p["minutes"])
        if minutes_total < 90: continue
        recent_minutes = [v for _, v in sorted(p["minutes"], reverse=True)[:5]]
        projected_minutes = min(90.0, sum(recent_minutes) / len(recent_minutes))
        min90 = minutes_total / 90.0
        score_base = p["xg"] if p["has_xg"] else p["goals"]
        score_rate = (score_base + .12) / (min90 + 1.5)
        attack_factor = min(1.35, max(.7, expected_goals / 1.4))
        score_p = 1 - math.exp(-score_rate * projected_minutes / 90 * attack_factor)
        assist_base = p["assists"] + .55 * p["xa"]
        assist_rate = (assist_base + .08) / (min90 + 1.5)
        assist_p = 1 - math.exp(-assist_rate * projected_minutes / 90 * attack_factor)
        common = dict(player=p["name"], player_id=pid, team=team, minutes=round(projected_minutes), appearances=p["matches"])
        score.append(common | dict(p=round(score_p, 4), basis="recent xG/goals and minutes"))
        assist.append(common | dict(p=round(assist_p, 4), basis="recent assists and xA/minutes"))
    return sorted(score, key=lambda x: -x["p"])[:3], sorted(assist, key=lambda x: -x["p"])[:3]

def team_recent_rates(records, team, venue, metric, role, baseline, prior=3):
    vals = []
    key = team_key(team)
    for row in records:
        side = "home" if row["home_key"] == key else "away" if row["away_key"] == key else None
        if not side or (venue != "any" and side != venue): continue
        pair = row.get(metric)
        if not pair: continue
        if role == "for": vals.append(pair[0] if side == "home" else pair[1])
        else: vals.append(pair[1] if side == "home" else pair[0])
    return ((sum(vals) + prior * baseline) / (len(vals) + prior), len(vals)) if baseline is not None else (None, len(vals))

def fixture_features(match, comp, schedules, details):
    pitch_match = pitch_match_for(match, comp, schedules)
    if not pitch_match: return None, None
    comp_rows = []
    for _, pm, stats in details.values():
        if _ != comp: continue
        dt = pitch_date(pm)
        if not dt or dt >= match["date"]: continue
        home, away = pm.get("home_team") or {}, pm.get("away_team") or {}
        comp_rows.append(dict(date=dt, home_key=team_key(home.get("name", "")), away_key=team_key(away.get("name", "")),
                              corners=stats.get("corners"), cards=stats.get("cards")))
    comp_rows.sort(key=lambda x: x["date"], reverse=True)
    # League averages provide a conservative prior when only a few team records are available.
    corner_home = [r["corners"][0] for r in comp_rows if r.get("corners")]
    corner_away = [r["corners"][1] for r in comp_rows if r.get("corners")]
    card_home = [r["cards"][0] for r in comp_rows if r.get("cards")]
    card_away = [r["cards"][1] for r in comp_rows if r.get("cards")]
    avg = lambda vals: sum(vals) / len(vals) if vals else None
    c_home_base, c_away_base = avg(corner_home), avg(corner_away)
    k_home_base, k_away_base = avg(card_home), avg(card_away)
    home_team, away_team = pitch_match.get("home_team") or {}, pitch_match.get("away_team") or {}
    features = {"stats_id": str(pitch_match.get("id", "")), "source": "PitchAPI", "source_updated": None}
    if c_home_base is not None and c_away_base is not None:
        hf, hn = team_recent_rates(comp_rows, match["home"], "home", "corners", "for", c_home_base)
        ad, an = team_recent_rates(comp_rows, match["away"], "away", "corners", "against", c_home_base)
        af, an2 = team_recent_rates(comp_rows, match["away"], "away", "corners", "for", c_away_base)
        hd, hn2 = team_recent_rates(comp_rows, match["home"], "home", "corners", "against", c_away_base)
        if None not in (hf, ad, af, hd):
            ch, ca = max(0, (hf + ad) / 2), max(0, (af + hd) / 2)
            features["corners"] = dict(home=round(ch, 1), away=round(ca, 1), total=round(ch + ca, 1), samples=min(hn, an, an2, hn2))
    if k_home_base is not None and k_away_base is not None:
        hc, hn = team_recent_rates(comp_rows, match["home"], "home", "cards", "for", k_home_base)
        ac, an = team_recent_rates(comp_rows, match["away"], "away", "cards", "for", k_away_base)
        if hc is not None and ac is not None:
            features["cards"] = dict(home=round(hc, 1), away=round(ac, 1), total=round(hc + ac, 1), samples=min(hn, an))
    hscore, hassist = player_projections(match["home"], home_team.get("id"), match.get("xg", [1.3, 1.1])[0], details)
    ascore, aassist = player_projections(match["away"], away_team.get("id"), match.get("xg", [1.3, 1.1])[1], details)
    if hscore or ascore: features["scorers"] = {"home": hscore, "away": ascore}
    if hassist or aassist: features["assists"] = {"home": hassist, "away": aassist}
    return features, pitch_match

def poisson_sample(rnd, l):
    L, k, p = math.exp(-l), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= L: return k
        k += 1

def demo(now):
    rnd = random.Random(7)
    teams = ["Lions", "Wolves", "Rovers", "City", "Eagles", "Dynamo", "Titans", "Falcons", "Stars", "Comets"]
    att = {t: rnd.uniform(.8, 1.6) for t in teams}; dfn = {t: rnd.uniform(.8, 1.4) for t in teams}
    pairs = [(a, b) for a in teams for b in teams if a != b]; rnd.shuffle(pairs); ms = []
    for k, (a, b) in enumerate(pairs):
        if k < 80:
            g1, g2 = poisson_sample(rnd, 1.45 * att[a] * dfn[b]), poisson_sample(rnd, 1.15 * att[b] * dfn[a])
            h1, h2 = sum(rnd.random() < .44 for _ in range(g1)), sum(rnd.random() < .44 for _ in range(g2))
            ms.append(dict(id=k, date=now - timedelta(days=80 - k), status="FINISHED", home=a, away=b, ft=(g1, g2), ht=(h1, h2)))
        else:
            ms.append(dict(id=k, date=now + timedelta(days=1 + (k - 80) % 3, hours=3), status="TIMED", home=a, away=b, ft=None, ht=None))
    return ms

# ---------- model ----------
def fit(fin, now):
    sw = sh = sa = h1 = tg = 0.0; T = {}
    t = lambda n: T.setdefault(n, dict(hw=0, hf=0, ha=0, aw=0, af=0, aa=0, res=[], recent=[]))
    for m in sorted(fin, key=lambda x: x["date"]):
        hg, ag = m["ft"]; w = math.exp(-XI * max(0, (now - m["date"]).days))
        sw += w; sh += w * hg; sa += w * ag
        if m["ht"]: h1 += sum(m["ht"]); tg += hg + ag
        a, b = t(m["home"]), t(m["away"])
        a["hw"] += w; a["hf"] += w * hg; a["ha"] += w * ag
        b["aw"] += w; b["af"] += w * ag; b["aa"] += w * hg
        a["res"].append(3 if hg > ag else 1 if hg == ag else 0)
        b["res"].append(3 if ag > hg else 1 if hg == ag else 0)
        a["recent"].append(dict(date=m["date"].date().isoformat(), opponent=m["away"], score=f"{hg}-{ag}", result="W" if hg > ag else "D" if hg == ag else "L"))
        b["recent"].append(dict(date=m["date"].date().isoformat(), opponent=m["home"], score=f"{ag}-{hg}", result="W" if ag > hg else "D" if ag == hg else "L"))
    share = min(.5, max(.38, h1 / tg)) if tg >= 50 else .45
    return dict(T=T, LH=sh / sw, LA=sa / sw, share=share)

def predict(M, home, away):
    T, LH, LA = M["T"], M["LH"], M["LA"]; h, a = T.get(home), T.get(away)
    if not h or not a: return None
    sm = lambda v, n, b: (v + K * b) / (n + K)
    hatt, hdef = sm(h["hf"], h["hw"], LH), sm(h["ha"], h["hw"], LA)
    aatt, adef = sm(a["af"], a["aw"], LA), sm(a["aa"], a["aw"], LH)
    ppg = lambda t: sum(t["res"][-5:]) / max(1, len(t["res"][-5:]))
    fh, fa = 1 + (ppg(h) - 1.5) / 1.5 * .06, 1 + (ppg(a) - 1.5) / 1.5 * .06
    lh = min(4, max(.2, hatt * adef / LH * fh)); la = min(4, max(.2, aatt * hdef / LA * fa))
    ph, pa = pmf(lh, 9), pmf(la, 9); P = {"1": 0, "X": 0, "2": 0}; ov = tot = 0; best = (0, 0, 0)
    for i in range(10):
        for j in range(10):
            p = ph[i] * pa[j]; tot += p; P[r3(i, j)] += p
            if i + j > 2.5: ov += p
            if p > best[2]: best = (i, j, p)
    P = {k: v / tot for k, v in P.items()}; ov /= tot
    s = M["share"]; a1, b1, a2, b2 = pmf(s * lh, 5), pmf(s * la, 5), pmf((1 - s) * lh, 5), pmf((1 - s) * la, 5)
    H = {}; ht = 0
    for x in range(6):
        for y in range(6):
            for c in range(6):
                for d in range(6):
                    p = a1[x] * b1[y] * a2[c] * b2[d]; k = r3(x, y) + "/" + r3(x + c, y + d); H[k] = H.get(k, 0) + p; ht += p
    H = {k: v / ht for k, v in H.items()}
    recent = (h["recent"][-5:][::-1], a["recent"][-5:][::-1])
    return dict(lh=lh, la=la, P=P, ov=ov, H=H, score=f"{best[0]}-{best[1]}", form=(ppg(h), ppg(a)), recent=recent,
                att=(hatt, aatt), dfn=(hdef, adef))

def _audit_bucket():
    return dict(n=0, hits=0, top_probability=0.0, brier=0.0, log_loss=0.0,
                baseline_brier=0.0, baseline_log_loss=0.0)

def _audit_baseline(rows, keys, label):
    counts = {k: 1.0 for k in keys}
    for row in rows:
        outcome = label(row)
        if outcome in counts: counts[outcome] += 1
    total = sum(counts.values())
    return {k: v / total for k, v in counts.items()}

def _audit_record(bucket, probs, actual, baseline):
    if actual not in probs or not probs: return
    total = sum(max(0.0, float(v)) for v in probs.values())
    base_total = sum(max(0.0, float(v)) for v in baseline.values())
    if total <= 0 or base_total <= 0: return
    p = {k: max(0.0, float(v)) / total for k, v in probs.items()}
    b = {k: max(0.0, float(baseline.get(k, 0.0))) / base_total for k in p}
    keys = list(p)
    bucket["n"] += 1
    bucket["hits"] += int(max(p, key=p.get) == actual)
    bucket["top_probability"] += max(p.values())
    bucket["brier"] += sum((p[k] - (1.0 if k == actual else 0.0)) ** 2 for k in keys)
    bucket["log_loss"] -= math.log(max(1e-12, p[actual]))
    bucket["baseline_brier"] += sum((b[k] - (1.0 if k == actual else 0.0)) ** 2 for k in keys)
    bucket["baseline_log_loss"] -= math.log(max(1e-12, b[actual]))

def _audit_market_snapshot(bucket):
    n = bucket["n"]
    if not n: return None
    brier = bucket["brier"] / n
    baseline_brier = bucket["baseline_brier"] / n
    return dict(n=n, top_pick_hit_rate=round(bucket["hits"] / n, 4),
                avg_top_pick_probability=round(bucket["top_probability"] / n, 4),
                brier=round(brier, 4), log_loss=round(bucket["log_loss"] / n, 4),
                baseline_brier=round(baseline_brier, 4),
                baseline_log_loss=round(bucket["baseline_log_loss"] / n, 4),
                brier_skill=round(1 - brier / baseline_brier, 4) if baseline_brier else None)

def _audit_score_bucket():
    return dict(n=0, hits=0, modal_probability=0.0, log_loss=0.0, modes={})

def _log_poisson(lam, goals):
    return -lam + goals * math.log(lam) - math.lgamma(goals + 1)

def _audit_score_record(bucket, prediction, actual):
    predicted = tuple(int(x) for x in prediction["score"].split("-"))
    home_goals, away_goals = actual
    lp = _log_poisson(prediction["lh"], home_goals) + _log_poisson(prediction["la"], away_goals)
    lm = _log_poisson(prediction["lh"], predicted[0]) + _log_poisson(prediction["la"], predicted[1])
    bucket["n"] += 1
    bucket["hits"] += int(predicted == tuple(actual))
    bucket["modal_probability"] += math.exp(lm)
    bucket["log_loss"] -= lp
    label = f"{predicted[0]}-{predicted[1]}"
    bucket["modes"][label] = bucket["modes"].get(label, 0) + 1

def _audit_score_snapshot(bucket):
    n = bucket["n"]
    if not n: return None
    mode, count = max(bucket["modes"].items(), key=lambda row: row[1])
    return dict(n=n, top_score_hit_rate=round(bucket["hits"] / n, 4),
                avg_modal_score_probability=round(bucket["modal_probability"] / n, 4),
                exact_score_log_loss=round(bucket["log_loss"] / n, 4),
                most_common_predicted_score=mode, most_common_predicted_score_count=count)

def walk_forward_audit(fin_by_comp, now, demo=False):
    overall_markets, overall_score = {}, _audit_score_bucket()
    competition_rows, test_dates, total_test_matches = [], [], 0
    market_keys = {
        "1X2": ["1", "X", "2"],
        "OU": ["O", "U"],
        "BTTS": ["Y", "N"],
    }
    for comp, raw in sorted(fin_by_comp.items()):
        finished = sorted((m for m in raw if m.get("ft") and m["date"] < now), key=lambda m: m["date"])
        local_markets, local_score, local_tested, local_dates = {}, _audit_score_bucket(), 0, []
        for match in finished:
            day = match["date"].date()
            # Exclude same-day results to keep the replay conservative and leak-free.
            prior = [m for m in finished if m["date"].date() < day]
            if len(prior) < MIN_MATCHES: continue
            model = predict(fit(prior, match["date"]), match["home"], match["away"])
            if not model: continue
            local_tested += 1
            local_dates.append(day.isoformat())
            test_dates.append(day.isoformat())
            actual_1x2 = r3(*match["ft"])
            labels = {
                "1X2": lambda row: r3(*row["ft"]),
                "OU": lambda row: "O" if sum(row["ft"]) >= 3 else "U",
                "BTTS": lambda row: "Y" if row["ft"][0] > 0 and row["ft"][1] > 0 else "N",
            }
            model_probs = {
                "1X2": model["P"],
                "OU": {"O": model["ov"], "U": 1 - model["ov"]},
                "BTTS": {"Y": (1 - math.exp(-model["lh"])) * (1 - math.exp(-model["la"])),
                         "N": 1 - (1 - math.exp(-model["lh"])) * (1 - math.exp(-model["la"]))},
            }
            for market, keys in market_keys.items():
                base = _audit_baseline(prior, keys, labels[market])
                actual = labels[market](match)
                for target in (local_markets, overall_markets):
                    _audit_record(target.setdefault(market, _audit_bucket()), model_probs[market], actual, base)
            ht_prior = [row for row in prior if row.get("ht")]
            if match.get("ht"):
                ht_label = lambda row: r3(*row["ht"]) + "/" + r3(*row["ft"])
                ht_keys = list(model["H"])
                ht_base = _audit_baseline(ht_prior, ht_keys, ht_label)
                ht_actual = ht_label(match)
                for target in (local_markets, overall_markets):
                    _audit_record(target.setdefault("HTFT", _audit_bucket()), model["H"], ht_actual, ht_base)
            _audit_score_record(local_score, model, match["ft"])
            _audit_score_record(overall_score, model, match["ft"])
        total_test_matches += local_tested
        if local_tested:
            competition_rows.append(dict(
                competition=comp, test_matches=local_tested,
                period=dict(from_date=min(local_dates), to_date=max(local_dates)),
                markets={k: value for k, bucket in local_markets.items()
                         if (value := _audit_market_snapshot(bucket)) is not None},
                scoreline=_audit_score_snapshot(local_score)))
    all_market_rows = {k: value for k, bucket in overall_markets.items()
                       if (value := _audit_market_snapshot(bucket)) is not None}
    return dict(
        updated=now.isoformat(), demo=demo, minimum_prior_matches=MIN_MATCHES,
        method="Rolling-date backtest on active-season results. Each forecast uses only finished matches from earlier calendar days in the same competition; same-day results are excluded. Baseline is the competition's smoothed prior outcome rate, without team information.",
        period=dict(from_date=min(test_dates), to_date=max(test_dates)) if test_dates else None,
        test_matches=total_test_matches,
        overall=dict(markets=all_market_rows, scoreline=_audit_score_snapshot(overall_score)),
        competitions=competition_rows)

def candidates(m, r):
    mt = f"{m['home']} vs {m['away']}"; P, c = r["P"], []
    k = max(P, key=P.get)
    c.append(dict(match=mt, mk="1X2", key=k, pick=f"{NM[k]} ({k})", p=P[k], why=(
        f"Expected goals: {m['home']} {r['lh']:.2f}, {m['away']} {r['la']:.2f}. Venue-adjusted attack/defence (home {r['att'][0]:.2f} scored/{r['dfn'][0]:.2f} conceded; "
        f"away {r['att'][1]:.2f} scored/{r['dfn'][1]:.2f} conceded) plus recent form ({r['form'][0]:.1f} vs {r['form'][1]:.1f} pts/game last 5). "
        f"Simulated chances: {pc(P['1'])} / {pc(P['X'])} / {pc(P['2'])}.")))
    dc = {"1X": P["1"] + P["X"], "X2": P["X"] + P["2"], "12": P["1"] + P["2"]}
    dc_names = {"1X": "Home or draw", "X2": "Draw or away", "12": "Home or away"}
    dk = max(dc, key=dc.get)
    c.append(dict(match=mt, mk="DC", key=dk, pick=f"Double chance: {dc_names[dk]} ({dk})", p=dc[dk], why=(
        f"Double chance covers two 1X2 outcomes. Model probabilities are home {pc(P['1'])}, draw {pc(P['X'])}, away {pc(P['2'])}; "
        f"{dc_names[dk]} has the highest combined probability at {pc(dc[dk])}.")))
    o = r["ov"] >= .5
    c.append(dict(match=mt, mk="OU", key="O" if o else "U", pick="Over 2.5" if o else "Under 2.5", p=r["ov"] if o else 1 - r["ov"], why=(
        f"Combined expected goals {r['lh'] + r['la']:.2f}. Probability of 3+ goals is {pc(r['ov'])}, so {'Over' if o else 'Under'} 2.5 is the likelier side.")))
    btts_yes = (1 - math.exp(-r["lh"])) * (1 - math.exp(-r["la"]))
    bk = "Y" if btts_yes >= .5 else "N"
    btts_p = btts_yes if bk == "Y" else 1 - btts_yes
    c.append(dict(match=mt, mk="BTTS", key=bk, pick=f"Both teams to score: {'Yes' if bk == 'Y' else 'No'}", p=btts_p, why=(
        f"Estimated goal means are {m['home']} {r['lh']:.2f} and {m['away']} {r['la']:.2f}. Under independent Poisson scoring, "
        f"both teams scoring has probability {pc(btts_yes)}; the selected side is {'Yes' if bk == 'Y' else 'No'}.")))
    h = max(r["H"], key=r["H"].get); x, y = h.split("/")
    c.append(dict(match=mt, mk="HTFT", key=h, pick=f"HT {NM[x]} / FT {NM[y]} ({h})", p=r["H"][h], why=(
        f"Each half is simulated separately using the league's real first-half goal share. {h} is the most likely half-time/full-time pair; "
        f"combos need two things to land, so they are riskier.")))
    return c

def player_candidates(m, features):
    out = []
    for market, field, label in (("SCORER", "scorers", "to score"), ("ASSIST", "assists", "to assist")):
        for side in ("home", "away"):
            rows = ((features or {}).get(field) or {}).get(side, [])
            if not rows: continue
            p = rows[0]
            team = m["home"] if side == "home" else m["away"]
            out.append(dict(match=f"{m['home']} vs {m['away']}", mk=market, key=p["player_id"], player_id=p["player_id"],
                            player=p["player"], team=team, pick=f"{p['player']} ({team}) {label}", p=p["p"],
                            why=f"Recent {p['basis']}; projected around {p['minutes']} minutes per appearance across {p['appearances']} recent appearances. Lineups and availability may change."))
    return out

def slip(all_c, lo, hi, n, mid):
    used, out = set(), []
    def take(arr):
        for c in arr:
            if len(out) >= n: break
            if c["match"] not in used: used.add(c["match"]); out.append(c)
    take(sorted([c for c in all_c if lo <= c["p"] < hi], key=lambda c: -c["p"]))
    if len(out) < 2: take(sorted(all_c, key=lambda c: abs(c["p"] - mid)))
    cp = math.prod(c["p"] for c in out) if out else 0
    legs = [dict(c, odds=round(1 / c["p"], 2)) for c in out]
    return dict(legs=legs, prob=cp, odds=round(1 / cp, 2) if cp else None)

def daily_slip(all_c, matches, now):
    # `matches` contains rendered prediction records, whose kickoff is stored as
    # an ISO string. Accept normalized source records too for local/demo use.
    def kickoff(match):
        value = match.get("kickoff") or match.get("date")
        if isinstance(value, datetime): return value
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    upcoming = [m for m in matches if kickoff(m) > now]
    if not upcoming: return None
    in_24h = [m for m in upcoming if kickoff(m) <= now + timedelta(hours=24)]
    first_day = min(kickoff(m) for m in upcoming).date()
    chosen = in_24h or [m for m in upcoming if kickoff(m).date() == first_day]
    ids = {m["id"] for m in chosen}; per_match = {}
    for c in all_c:
        if c.get("id") not in ids or c.get("mk") not in ("1X2", "OU", "DC", "BTTS"): continue
        old = per_match.get(c["id"])
        if old is None or c["p"] > old["p"]: per_match[c["id"]] = c
    picks = sorted(per_match.values(), key=lambda c: -c["p"])[:3]
    if not picks: return None
    prob = math.prod(c["p"] for c in picks)
    return dict(date=min(kickoff(m) for m in chosen).date().isoformat(), updated=now.isoformat(), legs=picks,
                prob=prob, odds=round(1 / prob, 2), note="Each leg is a model estimate. Combined probability assumes separate match outcomes and is approximate.")

# ---------- track record ----------
def evaluate(pk, res):
    ft, ht = res["ft"], res.get("ht")
    if pk["mk"] == "1X2": return int(pk["key"] == r3(*ft))
    if pk["mk"] == "OU": return int(pk["key"] == ("O" if sum(ft) > 2.5 else "U"))
    if pk["mk"] == "DC":
        result = r3(*ft); covered = {"1X": ("1", "X"), "X2": ("X", "2"), "12": ("1", "2")}
        return int(result in covered.get(pk["key"], ()))
    if pk["mk"] == "BTTS": return int(pk["key"] == ("Y" if ft[0] > 0 and ft[1] > 0 else "N"))
    if pk["mk"] == "HTFT": return None if not ht else int(pk["key"] == r3(*ht) + "/" + r3(*ft))
    if pk["mk"] in ("SCORER", "ASSIST"):
        if "players" not in res: return None
        player = (res.get("players") or {}).get(str(pk.get("player_id", pk.get("key"))))
        if player is None: return 0
        stat = "goals" if pk["mk"] == "SCORER" else "assists"
        return int((player.get(stat) or 0) > 0)

def summarize(items):
    S, br, n, errors = {}, 0.0, 0, {}
    for it in items:
        if not it.get("result"): continue
        for pk in it["picks"]:
            if pk.get("hit") is None: continue
            s = S.setdefault(pk["mk"], dict(market=pk["mk"], n=0, hits=0, psum=0.0)); s["n"] += 1; s["hits"] += pk["hit"]; s["psum"] += pk["p"]
        o = r3(*it["result"]["ft"]); br += sum((it["p1x2"][k] - (k == o)) ** 2 for k in "1X2"); n += 1
    rows = [dict(market=s["market"], n=s["n"], hit_rate=s["hits"] / s["n"], avg_prob=s["psum"] / s["n"]) for s in S.values()]
    for it in items:
        forecast, result = it.get("stats_prediction") or {}, (it.get("result") or {}).get("stats") or {}
        for market in ("corners", "cards"):
            predicted = (forecast.get(market) or {}).get("total"); actual = (result.get(market) or {}).get("total")
            if predicted is None or actual is None: continue
            s = errors.setdefault(market, dict(market=market, n=0, absolute_error=0.0)); s["n"] += 1; s["absolute_error"] += abs(predicted - actual)
    stats = [dict(market=s["market"], n=s["n"], mae=s["absolute_error"] / s["n"]) for s in errors.values()]
    return dict(markets=rows, stats=stats, matches=n, brier=br / n if n else None)

def main():
    dm = "--demo" in sys.argv; key = os.environ.get("FOOTBALL_DATA_KEY", "")
    if not dm and not key: sys.exit("Set FOOTBALL_DATA_KEY (free key from football-data.org)")
    now = datetime.now(timezone.utc); os.makedirs(OUT, exist_ok=True)
    hp = os.path.join(OUT, "history.json")
    hist = json.load(open(hp)) if os.path.exists(hp) else {"items": []}
    hist.setdefault("items", []); hist.setdefault("daily_slips", [])
    known = {i["id"] for i in hist["items"]}; finished = {}; matches = []; all_c = []; notes = []; audit_inputs = {}
    # Optional player and match-stat predictions are disabled until provider access works.
    pitch_key = ""
    pitch_data = dict(details={}, schedules={}, notes=[])
    for code, name in ({"DEMO": "Demo League"} if dm else COMPS).items():
        try:
            ms = demo(now) if dm else [norm(x) for x in fetch(code, key)]
        except Exception as e:
            notes.append(f"{name}: fetch failed ({e})"); continue
        if not dm: time.sleep(7)  # free API limit: 10 requests/minute
        fin = [m for m in ms if m["status"] == "FINISHED" and m["ft"]]
        audit_inputs[name] = fin
        for m in fin: finished[m["id"]] = m
        # The competition endpoint returns the active season's schedule. Show
        # every future fixture, but only lock a track-record pick close to kickoff.
        up = [m for m in ms if m["status"] in ("SCHEDULED", "TIMED") and now < m["date"]]
        if len(fin) < MIN_MATCHES:
            if up: notes.append(f"{name}: only {len(fin)} finished games so far, skipped"); continue
            continue
        M = fit(fin, now)
        for m in sorted(up, key=lambda x: x["date"]):
            r = predict(M, m["home"], m["away"])
            if not r: continue
            cs = candidates(m, r)
            model_match = m | {"xg": [r["lh"], r["la"]]}
            extra, pitch_match = (None, None) if dm or not pitch_key else fixture_features(model_match, name, pitch_data["schedules"], pitch_data["details"])
            if extra: cs.extend(player_candidates(m, extra))
            for c in cs: c.update(id=m["id"], comp=name, kickoff=m["date"].isoformat())
            if m["date"] <= now + timedelta(days=BETSLIP_DAYS): all_c += [c for c in cs if c["mk"] in ("1X2", "OU", "HTFT", "DC", "BTTS")]
            ent = dict(id=m["id"], comp=name, status=m["status"], kickoff=m["date"].isoformat(), home=m["home"], away=m["away"],
                       xg=[round(r["lh"], 2), round(r["la"], 2)], p1x2={k: round(v, 4) for k, v in r["P"].items()},
                       over25=round(r["ov"], 4), score=r["score"], recent=r["recent"],
                       htft=sorted(([k, round(v, 4)] for k, v in r["H"].items()), key=lambda z: -z[1])[:3], picks=cs, extra=extra)
            matches.append(ent)
            if m["date"] <= now + timedelta(days=TRACK_DAYS) and m["id"] not in known:  # lock close to kickoff
                hist["items"].append(dict(id=m["id"], comp=name, kickoff=ent["kickoff"], home=m["home"], away=m["away"], p1x2=ent["p1x2"],
                                          stats_id=(extra or {}).get("stats_id"),
                                          stats_prediction={k: extra[k] for k in ("corners", "cards") if extra and extra.get(k)},
                                          picks=[dict(mk=c["mk"], key=c["key"], player_id=c.get("player_id"), pick=c["pick"], p=round(c["p"], 4)) for c in cs], result=None))
                known.add(m["id"])
    for it in hist["items"]:
        f = finished.get(it["id"])
        if not f: continue
        if not it.get("result"): it["result"] = dict(ft=list(f["ft"]), ht=list(f["ht"]) if f["ht"] else None)
        stats_id = str(it.get("stats_id") or "")
        detail = pitch_data.get("details", {}).get(stats_id)
        if detail:
            stat_row = detail[2]
            player_map = {str(p["id"]): {"goals": p.get("goals", 0), "assists": p.get("assists", 0)} for p in stat_row.get("players", [])}
            it["result"]["players"] = player_map
            if stat_row.get("corners") is not None: it["result"]["stats"] = {**(it["result"].get("stats") or {}), "corners": dict(home=stat_row["corners"][0], away=stat_row["corners"][1], total=sum(stat_row["corners"]))}
            if stat_row.get("cards") is not None: it["result"]["stats"] = {**(it["result"].get("stats") or {}), "cards": dict(home=stat_row["cards"][0], away=stat_row["cards"][1], total=sum(stat_row["cards"]))}
        for pk in it["picks"]:
            value = evaluate(pk, it["result"])
            if value is not None: pk["hit"] = value
    slip_now = None if dm else daily_slip(all_c, matches, now)
    if slip_now:
        old = next((x for x in hist["daily_slips"] if x.get("date") == slip_now["date"]), None)
        if old is None:
            locked = {**slip_now, "legs": [dict(c, hit=None) for c in slip_now["legs"]], "hit": None}
            hist["daily_slips"].append(locked)
            old = locked
        slip_now = old
    for daily in hist["daily_slips"]:
        for leg in daily.get("legs", []):
            f = finished.get(leg.get("id"))
            if f and leg.get("hit") is None: leg["hit"] = evaluate(leg, {"ft": list(f["ft"]), "ht": list(f["ht"]) if f.get("ht") else None})
        hits = [leg.get("hit") for leg in daily.get("legs", [])]
        daily["hit"] = int(all(x == 1 for x in hits)) if hits and all(isinstance(x, int) for x in hits) else None
    audit = walk_forward_audit(audit_inputs, now, dm)
    json.dump(audit, open(os.path.join(OUT, "audit.json"), "w"), indent=1)
    hist["summary"] = summarize(hist["items"]); hist["updated"] = now.isoformat()
    json.dump(hist, open(hp, "w"), indent=1)
    pred = dict(updated=now.isoformat(), notes=notes, matches=matches, demo=dm, slip_of_day=slip_now,
                slips=dict(small=slip(all_c, .62, 1.01, 3, .7), medium=slip(all_c, .48, .62, 4, .55), big=slip(all_c, .2, .48, 5, .38)))
    json.dump(pred, open(os.path.join(OUT, "predictions.json"), "w"), indent=1, default=str)
    print(f"{len(matches)} matches predicted; {hist['summary']['matches']} resolved in track record; notes: {notes}")

if __name__ == "__main__":
    main()
