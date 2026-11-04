#!/usr/bin/env python3
"""Football predictor. Fetch results -> fit own model -> predictions.json + public track record.
Usage:  python scripts/update.py          (needs env FOOTBALL_DATA_KEY)
        python scripts/update.py --demo   (synthetic data, to test offline)
Uses only the Python standard library. No bookmaker odds or outside predictions are used."""
import os, sys, json, math, time, random
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

COMPS = {"PL": "Premier League", "PD": "La Liga", "SA": "Serie A", "BL1": "Bundesliga", "FL1": "Ligue 1"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "data")
XI = 0.006          # time decay per day: recent games count more
K = 3.0             # shrinkage: pulls small samples toward league average
TRACK_DAYS = 4      # lock track-record picks only this close to kickoff
BETSLIP_DAYS = 7    # keep betslips focused on the next week
MIN_MATCHES = 20    # skip a league until enough games were played
NM = {"1": "Home win", "X": "Draw", "2": "Away win"}

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
    t = lambda n: T.setdefault(n, dict(hw=0, hf=0, ha=0, aw=0, af=0, aa=0, res=[]))
    for m in sorted(fin, key=lambda x: x["date"]):
        hg, ag = m["ft"]; w = math.exp(-XI * max(0, (now - m["date"]).days))
        sw += w; sh += w * hg; sa += w * ag
        if m["ht"]: h1 += sum(m["ht"]); tg += hg + ag
        a, b = t(m["home"]), t(m["away"])
        a["hw"] += w; a["hf"] += w * hg; a["ha"] += w * ag
        b["aw"] += w; b["af"] += w * ag; b["aa"] += w * hg
        a["res"].append(3 if hg > ag else 1 if hg == ag else 0)
        b["res"].append(3 if ag > hg else 1 if hg == ag else 0)
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
    return dict(lh=lh, la=la, P=P, ov=ov, H=H, score=f"{best[0]}-{best[1]}", form=(ppg(h), ppg(a)),
                att=(hatt, aatt), dfn=(hdef, adef))

def candidates(m, r):
    mt = f"{m['home']} vs {m['away']}"; P, c = r["P"], []
    k = max(P, key=P.get)
    c.append(dict(match=mt, mk="1X2", key=k, pick=f"{NM[k]} ({k})", p=P[k], why=(
        f"Expected goals: {m['home']} {r['lh']:.2f}, {m['away']} {r['la']:.2f}. Venue-adjusted attack/defence (home {r['att'][0]:.2f} scored/{r['dfn'][0]:.2f} conceded; "
        f"away {r['att'][1]:.2f} scored/{r['dfn'][1]:.2f} conceded) plus recent form ({r['form'][0]:.1f} vs {r['form'][1]:.1f} pts/game last 5). "
        f"Simulated chances: {pc(P['1'])} / {pc(P['X'])} / {pc(P['2'])}.")))
    o = r["ov"] >= .5
    c.append(dict(match=mt, mk="OU", key="O" if o else "U", pick="Over 2.5" if o else "Under 2.5", p=r["ov"] if o else 1 - r["ov"], why=(
        f"Combined expected goals {r['lh'] + r['la']:.2f}. Probability of 3+ goals is {pc(r['ov'])}, so {'Over' if o else 'Under'} 2.5 is the likelier side.")))
    h = max(r["H"], key=r["H"].get); x, y = h.split("/")
    c.append(dict(match=mt, mk="HTFT", key=h, pick=f"HT {NM[x]} / FT {NM[y]} ({h})", p=r["H"][h], why=(
        f"Each half is simulated separately using the league's real first-half goal share. {h} is the most likely half-time/full-time pair; "
        f"combos need two things to land, so they are riskier.")))
    return c

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

# ---------- track record ----------
def evaluate(pk, res):
    ft, ht = res["ft"], res.get("ht")
    if pk["mk"] == "1X2": return int(pk["key"] == r3(*ft))
    if pk["mk"] == "OU": return int(pk["key"] == ("O" if sum(ft) > 2.5 else "U"))
    if pk["mk"] == "HTFT": return None if not ht else int(pk["key"] == r3(*ht) + "/" + r3(*ft))

def summarize(items):
    S, br, n = {}, 0.0, 0
    for it in items:
        if not it.get("result"): continue
        for pk in it["picks"]:
            if pk.get("hit") is None: continue
            s = S.setdefault(pk["mk"], dict(market=pk["mk"], n=0, hits=0, psum=0.0)); s["n"] += 1; s["hits"] += pk["hit"]; s["psum"] += pk["p"]
        o = r3(*it["result"]["ft"]); br += sum((it["p1x2"][k] - (k == o)) ** 2 for k in "1X2"); n += 1
    rows = [dict(market=s["market"], n=s["n"], hit_rate=s["hits"] / s["n"], avg_prob=s["psum"] / s["n"]) for s in S.values()]
    return dict(markets=rows, matches=n, brier=br / n if n else None)

def main():
    dm = "--demo" in sys.argv; key = os.environ.get("FOOTBALL_DATA_KEY", "")
    if not dm and not key: sys.exit("Set FOOTBALL_DATA_KEY (free key from football-data.org)")
    now = datetime.now(timezone.utc); os.makedirs(OUT, exist_ok=True)
    hp = os.path.join(OUT, "history.json")
    hist = json.load(open(hp)) if os.path.exists(hp) else {"items": []}
    known = {i["id"] for i in hist["items"]}; finished = {}; matches = []; all_c = []; notes = []
    for code, name in ({"DEMO": "Demo League"} if dm else COMPS).items():
        try:
            ms = demo(now) if dm else [norm(x) for x in fetch(code, key)]
        except Exception as e:
            notes.append(f"{name}: fetch failed ({e})"); continue
        if not dm: time.sleep(7)  # free API limit: 10 requests/minute
        fin = [m for m in ms if m["status"] == "FINISHED" and m["ft"]]
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
            if m["date"] <= now + timedelta(days=BETSLIP_DAYS): all_c += cs
            ent = dict(id=m["id"], comp=name, kickoff=m["date"].isoformat(), home=m["home"], away=m["away"],
                       xg=[round(r["lh"], 2), round(r["la"], 2)], p1x2={k: round(v, 4) for k, v in r["P"].items()},
                       over25=round(r["ov"], 4), score=r["score"],
                       htft=sorted(([k, round(v, 4)] for k, v in r["H"].items()), key=lambda z: -z[1])[:3], picks=cs)
            matches.append(ent)
            if m["date"] <= now + timedelta(days=TRACK_DAYS) and m["id"] not in known:  # lock close to kickoff
                hist["items"].append(dict(id=m["id"], comp=name, kickoff=ent["kickoff"], home=m["home"], away=m["away"], p1x2=ent["p1x2"],
                                          picks=[dict(mk=c["mk"], key=c["key"], pick=c["pick"], p=round(c["p"], 4)) for c in cs], result=None))
                known.add(m["id"])
    for it in hist["items"]:
        f = finished.get(it["id"])
        if f and not it["result"]:
            it["result"] = dict(ft=list(f["ft"]), ht=list(f["ht"]) if f["ht"] else None)
            for pk in it["picks"]: pk["hit"] = evaluate(pk, it["result"])
    hist["summary"] = summarize(hist["items"]); hist["updated"] = now.isoformat()
    json.dump(hist, open(hp, "w"), indent=1)
    pred = dict(updated=now.isoformat(), notes=notes, matches=matches, demo=dm, slips=dict(
        small=slip(all_c, .62, 1.01, 3, .7), medium=slip(all_c, .48, .62, 4, .55), big=slip(all_c, .2, .48, 5, .38)))
    json.dump(pred, open(os.path.join(OUT, "predictions.json"), "w"), indent=1, default=str)
    print(f"{len(matches)} matches predicted; {hist['summary']['matches']} resolved in track record; notes: {notes}")

if __name__ == "__main__":
    main()
