#!/usr/bin/env python3
"""Litchfield Weather - live JSON export of all SQLite data, read-only."""
import sqlite3, json, os
from flask import Flask, abort, request, Response
from datetime import datetime

SECRET_PATH = "weatherdata-9f3k2"
PORT        = 5091
ROW_CAP     = 5000
MAX_LIMIT   = 200000

DATABASES = {
    "weather":    "/home/waterproject/weather_station/weather_data.db",
    "water_tank": "/home/waterproject/water_tank.db",
    "soil":       "/home/waterproject/soil.db",
}
# Auto-publish any other database in the home folder so everything on the Pi is
# readable without editing this list. (weather_data.db lives in a subfolder and
# is added explicitly above; water_tank/soil are already keyed, so no dupes.)
import glob as _glob
for _p in sorted(_glob.glob("/home/waterproject/*.db")):
    _stem = os.path.splitext(os.path.basename(_p))[0]
    if _stem not in DATABASES and _p not in DATABASES.values():
        DATABASES[_stem] = _p

STATUS_SOURCES = {
    "weather":    ["current_conditions", "weather_data"],
    "water_tank": ["tank_readings"],
    "soil":       ["readings"],
}

# climate aggregation source: 1893-present neighbor-median daily rainfall
RAIN_DB    = "weather"
RAIN_TABLE = "area_rain_daily"
RAIN_COL   = "precip_in"
RAIN_DATE  = "date"
LIVE_RAIN_TABLE = "weather_daily"  # current-year month-to-date, updated daily
YEAR_EXPR  = "CAST(strftime('%Y', \"" + RAIN_DATE + "\") AS INTEGER)"
MONTH_EXPR = "CAST(strftime('%m', \"" + RAIN_DATE + "\") AS INTEGER)"
LIVE_YEAR_EXPR  = YEAR_EXPR
LIVE_MONTH_EXPR = MONTH_EXPR
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

TIME_HINTS = ("ts", "time", "date", "day", "utc", "stamp", "yr", "year")

# live NWS alerts for the Litchfield area (forecast zone + county zone)
NWS_ZONES = ["ILZ064", "ILC135"]
NWS_UA = "ChipsWeather/1.0 (chipsweather.online)"

app = Flask(__name__)

def ro_connect(path):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    except sqlite3.OperationalError:
        con = sqlite3.connect(path, timeout=5)
        con.execute("PRAGMA query_only = ON;")
    con.row_factory = sqlite3.Row
    return con

def list_tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]

def dump_table(con, table, cap=ROW_CAP):
    cur = con.cursor()
    try:
        total = cur.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]
    except Exception as e:
        return {"error": str(e)}
    try:
        rows = cur.execute('SELECT * FROM "%s" ORDER BY rowid DESC LIMIT ?' % table,
                           (cap,)).fetchall()
    except Exception:
        rows = cur.execute('SELECT * FROM "%s" LIMIT ?' % table, (cap,)).fetchall()
    data = [dict(r) for r in rows]
    out = {"total_rows": total, "returned": len(data), "rows": data}
    if total > len(data):
        out["note"] = "showing %d most-recent rows of %d (add ?limit=N for more)" % (len(data), total)
    return out

def latest_row(con, table):
    try:
        r = con.execute('SELECT * FROM "%s" ORDER BY rowid DESC LIMIT 1' % table).fetchone()
        return dict(r) if r else None
    except Exception:
        return None

def table_columns(con, table):
    try:
        return [r[1] for r in con.execute('PRAGMA table_info("%s")' % table).fetchall()]
    except Exception:
        return []

def table_meta(con, table):
    cols = table_columns(con, table)
    meta = {"columns": cols}
    try:
        meta["rows"] = con.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]
    except Exception as e:
        meta["rows"] = None
        meta["error"] = str(e)
        return meta
    tcol = None
    for c in cols:
        if any(h in c.lower() for h in TIME_HINTS):
            tcol = c
            break
    if tcol:
        try:
            lo, hi = con.execute(
                'SELECT MIN("%s"), MAX("%s") FROM "%s"' % (tcol, tcol, table)
            ).fetchone()
            if lo is not None or hi is not None:
                meta["range_column"] = tcol
                meta["earliest"] = lo
                meta["latest"] = hi
        except Exception:
            pass
    return meta

def build_payload():
    payload = {"generated_utc": datetime.utcnow().isoformat() + "Z",
               "databases": {}, "missing_databases": []}
    for name, path in DATABASES.items():
        if not os.path.exists(path):
            payload["missing_databases"].append(name)
            continue
        try:
            con = ro_connect(path)
        except Exception as e:
            payload["databases"][name] = {"error": str(e)}
            continue
        payload["databases"][name] = {t: dump_table(con, t) for t in list_tables(con)}
        con.close()
    return payload

# ---------- climate layer ----------

def _rain_con():
    path = DATABASES.get(RAIN_DB)
    if not path or not os.path.exists(path):
        return None
    try:
        return ro_connect(path)
    except Exception:
        return None

def build_month_rankings(month):
    con = _rain_con()
    if con is None:
        return {"error": "rain source unavailable"}
    try:
        q = ('SELECT %s AS yr, ROUND(SUM("%s"),2) AS rain_in, MAX("%s") AS last_day '
             'FROM "%s" WHERE "%s" IS NOT NULL AND %s = ? '
             'GROUP BY yr ORDER BY yr'
             % (YEAR_EXPR, RAIN_COL, RAIN_DATE, RAIN_TABLE, RAIN_DATE, MONTH_EXPR))
        hist = {r["yr"]: {"rain_in": r["rain_in"], "last_day": r["last_day"]}
                for r in con.execute(q, (month,)).fetchall() if r["yr"] is not None}
    except Exception as e:
        con.close()
        return {"error": str(e)}

    # current-year month-to-date from the live daily table (updated through today)
    live_year = None
    live_total = None
    live_through = None
    try:
        lr = con.execute(
            'SELECT %s AS yr, ROUND(SUM("%s"),2) AS rain_in, MAX("%s") AS last_day '
            'FROM "%s" WHERE "%s" IS NOT NULL AND %s = ? '
            'GROUP BY yr ORDER BY yr DESC LIMIT 1'
            % (LIVE_YEAR_EXPR, RAIN_COL, RAIN_DATE, LIVE_RAIN_TABLE, RAIN_DATE, LIVE_MONTH_EXPR),
            (month,)).fetchone()
        if lr and lr["yr"] is not None:
            live_year, live_total, live_through = lr["yr"], lr["rain_in"], lr["last_day"]
    except Exception:
        pass
    con.close()

    # merge: live current-year value overrides the (stale) historical table for that year
    source_for_current = RAIN_TABLE
    if live_year is not None:
        if (live_year not in hist) or (live_total is not None):
            hist[live_year] = {"rain_in": live_total, "last_day": live_through}
            source_for_current = LIVE_RAIN_TABLE

    ranked = sorted(
        [{"year": y, "rain_in": v["rain_in"]} for y, v in hist.items()],
        key=lambda x: (-(x["rain_in"] if x["rain_in"] is not None else -1)))
    years = list(hist.keys())
    current_year = max(years) if years else None
    current_rain = hist.get(current_year, {}).get("rain_in") if current_year else None
    current_rank = next((i for i, r in enumerate(ranked, 1) if r["year"] == current_year), None)
    current_through = hist.get(current_year, {}).get("last_day") if current_year else None
    return {
        "generated_utc": datetime.utcnow().isoformat() + "Z",
        "history_source": RAIN_TABLE,
        "current_year_source": source_for_current,
        "month": month,
        "month_name": MONTH_NAMES[month] if 1 <= month <= 12 else str(month),
        "current_year": current_year,
        "current_rain_in": current_rain,
        "current_rank": current_rank,
        "current_year_through": current_through,
        "total_years": len(ranked),
        "note": ("Rank is among all years on record for this month (1 = wettest). "
                 "Historical years come from the 1893-present area record; the "
                 "current year's month-to-date comes from the live station table "
                 "and updates daily. current_year_through shows the last day counted."),
        "rankings": ranked,
    }

def build_june_rankings():
    m = build_month_rankings(6)
    if "error" in m:
        return m
    return {
        "generated_utc": m["generated_utc"],
        "history_source": m.get("history_source"),
        "current_year_source": m.get("current_year_source"),
        "current_year": m["current_year"],
        "current_june_rain_in": m["current_rain_in"],
        "current_rank": m["current_rank"],
        "current_year_through": m["current_year_through"],
        "total_years": m["total_years"],
        "note": m["note"],
        "top_years": m["rankings"][:10],
        "all_years_ranked": m["rankings"],
    }

def build_monthly_rain():
    con = _rain_con()
    if con is None:
        return {"error": "rain source unavailable"}
    try:
        q = ('SELECT %s AS yr, %s AS mo, ROUND(SUM("%s"),2) AS rain_in '
             'FROM "%s" WHERE "%s" IS NOT NULL GROUP BY yr, mo '
             'ORDER BY yr DESC, mo'
             % (YEAR_EXPR, MONTH_EXPR, RAIN_COL, RAIN_TABLE, RAIN_DATE))
        rows = con.execute(q).fetchall()
    except Exception as e:
        con.close()
        return {"error": str(e)}
    con.close()
    return [{"year": r["yr"], "month": r["mo"], "rain_in": r["rain_in"]}
            for r in rows if r["yr"] is not None]

def build_yearly_rain():
    con = _rain_con()
    if con is None:
        return {"error": "rain source unavailable"}
    try:
        q = ('SELECT %s AS yr, ROUND(SUM("%s"),2) AS rain_in '
             'FROM "%s" WHERE "%s" IS NOT NULL GROUP BY yr ORDER BY yr DESC'
             % (YEAR_EXPR, RAIN_COL, RAIN_TABLE, RAIN_DATE))
        rows = con.execute(q).fetchall()
    except Exception as e:
        con.close()
        return {"error": str(e)}
    con.close()
    return [{"year": r["yr"], "rain_in": r["rain_in"]}
            for r in rows if r["yr"] is not None]

def build_alerts():
    out = {"generated_utc": datetime.utcnow().isoformat() + "Z",
           "zones": NWS_ZONES, "active_count": 0, "alerts": []}
    try:
        import requests
    except Exception:
        out["error"] = "requests not available"
        return out
    seen = set()
    for z in NWS_ZONES:
        try:
            r = requests.get("https://api.weather.gov/alerts/active",
                             params={"zone": z},
                             headers={"User-Agent": NWS_UA,
                                      "Accept": "application/geo+json"},
                             timeout=6)
            if r.status_code != 200:
                continue
            feats = r.json().get("features", [])
        except Exception:
            continue
        for f in feats:
            p = f.get("properties", {}) or {}
            aid = p.get("id") or f.get("id")
            if aid and aid in seen:
                continue
            if aid:
                seen.add(aid)
            out["alerts"].append({
                "event": p.get("event"),
                "severity": p.get("severity"),
                "urgency": p.get("urgency"),
                "certainty": p.get("certainty"),
                "headline": p.get("headline"),
                "area": p.get("areaDesc"),
                "onset": p.get("onset"),
                "expires": p.get("expires"),
                "ends": p.get("ends"),
                "description": p.get("description"),
                "instruction": p.get("instruction"),
            })
    out["active_count"] = len(out["alerts"])
    return out

# ---------- safe read-only SQL query endpoint ----------
# Accepts a single SELECT against one named database. Locked down so it can only
# read: no writes, no multiple statements, no PRAGMA/ATTACH, hard row + time caps.

import re as _re

QUERY_ROW_CAP   = 5000     # max rows any single query may return
QUERY_TIMEOUT_S = 8        # kill a query that runs longer than this
_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "attach", "detach", "pragma", "vacuum", "reindex", "trigger",
    "commit", "rollback", "savepoint", "grant", "revoke", "load_extension",
)

def _query_is_safe(sql):
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False, "empty query"
    low = s.lower()
    # must start with SELECT or WITH (CTE that feeds a SELECT)
    if not (low.startswith("select") or low.startswith("with")):
        return False, "only SELECT queries are allowed"
    # no stacked statements (one trailing ; already stripped; any remaining = multiple)
    if ";" in s:
        return False, "only a single statement is allowed"
    # block dangerous keywords as whole words
    for kw in _FORBIDDEN:
        if _re.search(r"\b" + kw + r"\b", low):
            return False, "keyword not allowed: " + kw
    return True, s

def run_query(db, sql):
    if db not in DATABASES or not os.path.exists(DATABASES[db]):
        return {"error": "unknown database '%s'" % db,
                "available": list(DATABASES.keys())}
    ok, result = _query_is_safe(sql)
    if not ok:
        return {"error": result}
    safe_sql = result
    try:
        con = ro_connect(DATABASES[db])
    except Exception as e:
        return {"error": "could not open database: %s" % e}
    # hard time limit so a heavy query can't hang the Pi
    import time as _time
    deadline = _time.time() + QUERY_TIMEOUT_S
    def _guard():
        if _time.time() > deadline:
            return 1
        return 0
    try:
        con.set_progress_handler(_guard, 100000)
    except Exception:
        pass
    try:
        cur = con.execute(safe_sql)
        rows = cur.fetchmany(QUERY_ROW_CAP + 1)
        cols = [d[0] for d in cur.description] if cur.description else []
    except Exception as e:
        con.close()
        return {"error": "query failed: %s" % e, "database": db, "query": safe_sql}
    con.close()
    truncated = len(rows) > QUERY_ROW_CAP
    rows = rows[:QUERY_ROW_CAP]
    out = {
        "database": db,
        "query": safe_sql,
        "columns": cols,
        "row_count": len(rows),
        "rows": [dict(zip(cols, r)) for r in rows],
    }
    if truncated:
        out["note"] = "result truncated at %d rows; add LIMIT or aggregate" % QUERY_ROW_CAP
    return out

# ---------- named question endpoints (pre-built, listed in manifest) ----------
# Each computes on the Pi and returns a clean answer, so ChatGPT can hit a fixed
# URL instead of constructing SQL (its browser blocks self-built query URLs).

def _q1(db, sql, params=()):
    """Run an internal trusted SELECT, return list of dict rows (no caps/guards needed)."""
    if db not in DATABASES or not os.path.exists(DATABASES[db]):
        return None
    try:
        con = ro_connect(DATABASES[db])
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        con.close()
        return rows
    except Exception as e:
        return {"error": str(e)}

def build_records():
    """Headline records across the full record."""
    out = {"generated_utc": datetime.utcnow().isoformat() + "Z", "source": "weather"}
    wettest_day = _q1("weather",
        'SELECT "%s" AS date, "%s" AS rain_in FROM "%s" WHERE "%s" IS NOT NULL '
        'ORDER BY "%s" DESC LIMIT 1' % (RAIN_DATE, RAIN_COL, RAIN_TABLE, RAIN_COL, RAIN_COL))
    hottest = _q1("weather",
        'SELECT date, tmax_f FROM weather_daily WHERE tmax_f IS NOT NULL ORDER BY tmax_f DESC LIMIT 1')
    coldest = _q1("weather",
        'SELECT date, tmin_f FROM weather_daily WHERE tmin_f IS NOT NULL ORDER BY tmin_f ASC LIMIT 1')
    out["wettest_day_ever"] = (wettest_day or [None])[0]
    out["hottest_day_ever"] = (hottest or [None])[0]
    out["coldest_day_ever"] = (coldest or [None])[0]
    return out

def build_wettest_months(limit=10):
    rows = _q1("weather",
        'SELECT %s AS year, %s AS month, ROUND(SUM("%s"),2) AS rain_in '
        'FROM "%s" WHERE "%s" IS NOT NULL GROUP BY year, month '
        'ORDER BY rain_in DESC LIMIT ?' % (YEAR_EXPR, MONTH_EXPR, RAIN_COL, RAIN_TABLE, RAIN_DATE),
        (limit,))
    return {"generated_utc": datetime.utcnow().isoformat() + "Z",
            "wettest_months": rows}

def build_driest_years(limit=10):
    rows = _q1("weather",
        'SELECT %s AS year, ROUND(SUM("%s"),2) AS rain_in '
        'FROM "%s" WHERE "%s" IS NOT NULL GROUP BY year '
        'ORDER BY rain_in ASC LIMIT ?' % (YEAR_EXPR, RAIN_COL, RAIN_TABLE, RAIN_DATE),
        (limit,))
    return {"generated_utc": datetime.utcnow().isoformat() + "Z",
            "driest_years": rows}

def build_day_in_history(md=None):
    """How does a given calendar day (MM-DD) compare across the FULL record?
    Temps: deep history from area_temp_stations (1893-present, area-average), with
    a fallback to the station's own weather_daily (2004-present) if needed.
    Rain: area_rain_daily (1893-present). Plus the most recent actual reading."""
    if not md:
        md = datetime.utcnow().strftime("%m-%d")
    like = "%-" + md

    # deep temperature history (1893-present) from the area-average station table
    deep = _q1("weather",
        'SELECT ROUND(AVG(avg_tmax_f),1) AS avg_high, ROUND(AVG(avg_tmin_f),1) AS avg_low, '
        'MAX(avg_tmax_f) AS record_high, MIN(avg_tmin_f) AS record_low, '
        'COUNT(*) AS years, MIN(date) AS since '
        'FROM area_temp_stations WHERE date LIKE ?', (like,))
    deep_ok = isinstance(deep, list) and len(deep) > 0
    temps = deep[0] if deep_ok else None
    temp_source = "area_temp_stations (1893-present, area average)"
    if not temps or temps.get("years") in (None, 0):
        # fallback to the station's own record if the deep table is unavailable
        st = _q1("weather",
            'SELECT ROUND(AVG(tmax_f),1) AS avg_high, ROUND(AVG(tmin_f),1) AS avg_low, '
            'MAX(tmax_f) AS record_high, MIN(tmin_f) AS record_low, COUNT(*) AS years '
            'FROM weather_daily WHERE date LIKE ?', (like,))
        temps = (st or [None])[0]
        temp_source = "weather_daily (2004-present, station)"

    rain = _q1("weather",
        'SELECT ROUND(AVG("%s"),2) AS avg_rain, MAX("%s") AS record_rain, COUNT(*) AS years '
        'FROM "%s" WHERE date LIKE ?' % (RAIN_COL, RAIN_COL, RAIN_TABLE), (like,))

    today = _q1("weather",
        'SELECT date, tmax_f, tmin_f, precip_in FROM weather_daily WHERE date LIKE ? '
        'ORDER BY date DESC LIMIT 1', (like,))

    out = {"generated_utc": datetime.utcnow().isoformat() + "Z",
           "calendar_day": md,
           "temp_source": temp_source,
           "temp_normals_and_records": temps,
           "rain_source": RAIN_TABLE + " (1893-present)",
           "rain_normals": (rain or [None])[0],
           "most_recent_actual_reading": (today or [None])[0]}
    return out

# ---------- manifest / status ----------

def build_manifest():
    base = "/" + SECRET_PATH
    man = {
        "name": "Litchfield Weather Data",
        "description": ("Read-only live export of weather, water tank, and soil "
                        "data from a self-hosted Raspberry Pi station in "
                        "Litchfield, Illinois. Reflects the databases in real "
                        "time at the moment of each request."),
        "station": "KILLITCH54 (Weather Underground)",
        "generated_utc": datetime.utcnow().isoformat() + "Z",
        "how_to_use": {
            "everything": base,
            "current_summary": base + "/status.json",
            "one_table": base + "/{database}/{table}",
            "more_rows": base + "/{database}/{table}?limit=50000",
            "note": ("Tables return their %d most-recent rows by default, newest "
                     "first. Add ?limit=N (up to %d) to pull deeper history. "
                     "Read status.json for current conditions without a big "
                     "download. Use the climate_history endpoints for ranked "
                     "rainfall comparisons." % (ROW_CAP, MAX_LIMIT)),
        },
        "examples": {
            "manifest": base + "/manifest.json",
            "current_summary": base + "/status.json",
            "all_data": base,
            "active_alerts": base + "/alerts.json",
            "ask_a_query": base + "/query.json?db=weather&sql=SELECT date,tmax_f,tmin_f,precip_in FROM weather_daily ORDER BY date DESC LIMIT 7",
            "current_weather": base + "/weather/current_conditions",
            "daily_history": base + "/weather/weather_daily?limit=100000",
            "snow_history": base + "/weather/snow_daily?limit=100000",
            "tornado_prelim_counts": base + "/weather/illinois_tornado_prelim_years",
            "tornado_warnings": base + "/weather/warning_log",
            "water_tank": base + "/water_tank/tank_readings",
            "soil": base + "/soil/readings",
        },
        "climate_history": {
            "june_rankings": base + "/climate/june_rankings.json",
            "month_rankings_any": base + "/climate/month_rankings.json?month=6",
            "monthly_rain": base + "/climate/monthly_rain.json",
            "yearly_rain": base + "/climate/yearly_rain.json",
            "records": base + "/climate/records.json",
            "wettest_months": base + "/climate/wettest_months.json",
            "driest_years": base + "/climate/driest_years.json",
            "day_in_history": base + "/climate/day_in_history.json",
            "source_table": RAIN_TABLE,
            "note": ("Pre-aggregated rainfall from the 1893-present area "
                     "neighbor-median daily record. june_rankings ranks every "
                     "June by total rain and gives the current year's rank. "
                     "month_rankings accepts ?month=1..12 for any month. "
                     "These answer 'how wet is this month/year vs history' "
                     "without scanning raw daily rows."),
        },
        "databases": {},
        "missing_databases": [],
    }
    for name, path in DATABASES.items():
        if not os.path.exists(path):
            man["missing_databases"].append(name)
            continue
        try:
            con = ro_connect(path)
        except Exception as e:
            man["databases"][name] = {"error": str(e)}
            continue
        man["databases"][name] = {
            "url": base + "/" + name,
            "tables": {t: table_meta(con, t) for t in list_tables(con)},
        }
        con.close()
    return man

def build_status():
    status = {"generated_utc": datetime.utcnow().isoformat() + "Z", "current": {}}
    for name, candidates in STATUS_SOURCES.items():
        path = DATABASES.get(name)
        if not path or not os.path.exists(path):
            continue
        try:
            con = ro_connect(path)
        except Exception:
            continue
        present = set(list_tables(con))
        for table in candidates:
            if table in present:
                row = latest_row(con, table)
                if row is not None:
                    status["current"][name] = {"table": table, "latest": row}
                break
        con.close()
    al = build_alerts()
    status["alerts"] = {
        "active_count": al.get("active_count", 0),
        "summary": [{"event": a.get("event"), "expires": a.get("expires"),
                     "headline": a.get("headline")} for a in al.get("alerts", [])],
    }
    return status

def jresp(obj):
    r = Response(json.dumps(obj, default=str, indent=2),
                 mimetype="application/json")
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    r.headers["CDN-Cache-Control"] = "no-store"
    return r

@app.route("/" + SECRET_PATH)
def all_data():
    return jresp(build_payload())

@app.route("/" + SECRET_PATH + "/manifest")
@app.route("/" + SECRET_PATH + "/manifest.json")
def manifest():
    return jresp(build_manifest())

@app.route("/" + SECRET_PATH + "/status")
@app.route("/" + SECRET_PATH + "/status.json")
def status():
    return jresp(build_status())

@app.route("/" + SECRET_PATH + "/alerts")
@app.route("/" + SECRET_PATH + "/alerts.json")
def alerts():
    return jresp(build_alerts())

@app.route("/" + SECRET_PATH + "/query")
@app.route("/" + SECRET_PATH + "/query.json")
def query():
    db = request.args.get("db", "weather")
    sql = request.args.get("sql", "")
    if not sql:
        return jresp({"error": "provide a SELECT query in the ?sql= parameter, "
                              "and ?db= for the database",
                      "databases": list(DATABASES.keys()),
                      "example": "/%s/query.json?db=weather&sql=SELECT date,tmax_f "
                                 "FROM weather_daily ORDER BY date DESC LIMIT 5"
                                 % SECRET_PATH})
    return jresp(run_query(db, sql))

@app.route("/" + SECRET_PATH + "/climate/records.json")
def climate_records():
    return jresp(build_records())

@app.route("/" + SECRET_PATH + "/climate/wettest_months.json")
def climate_wettest_months():
    return jresp(build_wettest_months())

@app.route("/" + SECRET_PATH + "/climate/driest_years.json")
def climate_driest_years():
    return jresp(build_driest_years())

@app.route("/" + SECRET_PATH + "/climate/day_in_history.json")
def climate_day_in_history():
    return jresp(build_day_in_history(request.args.get("md")))

@app.route("/" + SECRET_PATH + "/climate/june_rankings")
@app.route("/" + SECRET_PATH + "/climate/june_rankings.json")
def climate_june():
    return jresp(build_june_rankings())

@app.route("/" + SECRET_PATH + "/climate/month_rankings")
@app.route("/" + SECRET_PATH + "/climate/month_rankings.json")
def climate_month():
    try:
        month = int(request.args.get("month", datetime.utcnow().month))
    except (TypeError, ValueError):
        month = datetime.utcnow().month
    if not (1 <= month <= 12):
        month = datetime.utcnow().month
    return jresp(build_month_rankings(month))

@app.route("/" + SECRET_PATH + "/climate/monthly_rain")
@app.route("/" + SECRET_PATH + "/climate/monthly_rain.json")
def climate_monthly():
    return jresp(build_monthly_rain())

@app.route("/" + SECRET_PATH + "/climate/yearly_rain")
@app.route("/" + SECRET_PATH + "/climate/yearly_rain.json")
def climate_yearly():
    return jresp(build_yearly_rain())

@app.route("/" + SECRET_PATH + "/<db>")
def one_db(db):
    if db not in DATABASES or not os.path.exists(DATABASES[db]):
        abort(404)
    con = ro_connect(DATABASES[db])
    out = {t: dump_table(con, t) for t in list_tables(con)}
    con.close()
    return jresp(out)

@app.route("/" + SECRET_PATH + "/<db>/<table>")
def one_table(db, table):
    if db not in DATABASES or not os.path.exists(DATABASES[db]):
        abort(404)
    try:
        cap = min(int(request.args.get("limit", ROW_CAP)), MAX_LIMIT)
    except (TypeError, ValueError):
        cap = ROW_CAP
    con = ro_connect(DATABASES[db])
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone():
        abort(404)
    out = dump_table(con, table, cap=cap)
    con.close()
    return jresp(out)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
