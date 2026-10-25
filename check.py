#!/usr/bin/env python3
"""Gridpoint live check.

Loads the app in a headless browser for places all over the U.S. (every time zone, coasts,
mountains, deserts, islands, the Arctic) and checks what it shows against the sources,
re-derived independently here in Python:

  * "Now": the newest valid report from the place's nearest stations (weather.gov + Iowa Mesonet)
  * Hourly strip: the NWS hourly forecast, hour by hour
  * Daily rows: NWS day/night periods (high, low, chance) and the extended-outlook model
    (NBM in the lower 48, NOAA GFS in Alaska/Hawaii/Puerto Rico), recomputed from hourly values
  * Sunrise/sunset: the `astral` library; moon phases: the `ephem` library
  * Radar loads, forecaster discussion present, charts draw and the readout scrubs,
    the page locks while a sheet is open, no console errors, no NaN/undefined on screen
  * Languages: every pack complete, each language on a phone screen, NWS wording translated,
    "Automatic" following the device, the language picker

Usage:
  python3 tests/check.py              # all places
  python3 tests/check.py --quick      # a 7-place sample
  python3 tests/check.py --only "Phoenix,Honolulu"
  python3 tests/check.py --json results.json
  python3 tests/check.py --url https://gridpoint.emersonxia.lol/   # test the live site
  python3 tests/check.py --only languages   # just the 14 languages (or: install, search)

Needs: pip install playwright astral ephem   (and Playwright's Chromium)
Exit code 1 if anything FAILs; WARN lines are worth a look but can be timing (a new
station report arriving between the app's fetch and this script's).
"""
import argparse, asyncio, datetime as dt, functools, http.server, json, math, os, re, socketserver, sys, threading, time, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
# the app's folder: next to this script (as in the repo), or one up (tests/check.py)
ROOT = HERE if os.path.exists(os.path.join(HERE, 'index.html')) else os.path.dirname(HERE)
UA = {'User-Agent': '(Gridpoint check, https://github.com/)', 'Accept': 'application/geo+json'}
HOUR = 3600

PLACES = [
    ("Northfield", "MN", 44.4620, -93.1830),
    ("New York", "NY", 40.7128, -74.0060),
    ("Miami", "FL", 25.7617, -80.1918),
    ("Key West", "FL", 24.5551, -81.7800),
    ("Denver", "CO", 39.7392, -104.9903),
    ("Phoenix", "AZ", 33.4484, -112.0740),          # no daylight saving time
    ("El Paso", "TX", 31.7619, -106.4850),           # Mountain time inside Texas
    ("Salt Lake City", "UT", 40.7608, -111.8910),
    ("Seattle", "WA", 47.6062, -122.3321),
    ("Millbrae", "CA", 37.5986, -122.3872),
    ("Furnace Creek", "CA", 36.4570, -116.8670),     # below sea level
    ("Boise", "ID", 43.6150, -116.2023),
    ("Fargo", "ND", 46.8772, -96.7898),
    ("Chicago", "IL", 41.8781, -87.6298),
    ("New Orleans", "LA", 29.9511, -90.0715),
    ("Indianapolis", "IN", 39.7684, -86.1581),
    ("Boston", "MA", 42.3601, -71.0589),
    ("Big Sky", "MT", 45.2847, -111.3683),           # mountains, far from an airport
    ("Ely", "NV", 39.2474, -114.8886),
    ("Cape Hatteras", "NC", 35.2510, -75.5290),      # barrier island
    ("Anchorage", "AK", 61.2181, -149.9003),
    ("Fairbanks", "AK", 64.8378, -147.7164),
    ("Utqiagvik", "AK", 71.2906, -156.7886),         # polar night from mid-November
    ("Adak", "AK", 51.8800, -176.6581),              # Hawaii-Aleutian time
    ("Honolulu", "HI", 21.3069, -157.8583),
    ("Hilo", "HI", 19.7074, -155.0885),
    ("San Juan", "PR", 18.4655, -66.1057),
]
QUICK = ["Northfield", "Phoenix", "Seattle", "Anchorage", "Honolulu", "San Juan", "Big Sky"]


# ---------------------------------------------------------------- fetching
def get_json(url, headers=None, tries=5):
    err = None
    for a in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or UA), timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            err = e
        except Exception as e:
            err = e
        time.sleep(1.5 * (a + 1))
    raise err


def upd(j):
    return ((j or {}).get('properties') or {}).get('updateTime') or ''


async def nws_versions(url, first, tries=4):
    """weather.gov answers from many servers, and for a while after each forecast update some of them still
    hand out the previous version. Fetch the product a few more times and return the distinct versions seen
    (the first one included), so a mismatch can be told apart from that."""
    seen = {(upd(first), ((first.get('properties') or {}).get('generatedAt'))): first}
    for _ in range(tries):
        await asyncio.sleep(1.5)
        try:
            j = await asyncio.to_thread(get_json, url)
        except Exception:
            continue
        seen.setdefault((upd(j), j['properties'].get('generatedAt')), j)
    return list(seen.values())


def hhmm(iso):
    return iso[11:16] + 'Z' if iso else '?'


def c2f(c):
    return None if c is None else c * 9 / 5 + 32


def expected_now(stations):
    """The app's rule: nearest of the first four stations with a valid temperature;
    the newer of weather.gov (<3 h old) and the Iowa Mesonet (<2 h old)."""
    now = time.time()
    nws = None
    for sid in stations[:4]:
        try:
            j = get_json(f'https://api.weather.gov/stations/{sid}/observations?limit=6')
        except Exception:
            continue
        for f in j.get('features', []):
            p = f['properties']
            if p.get('temperature', {}).get('value') is not None:
                t = dt.datetime.fromisoformat(p['timestamp']).timestamp()
                if now - t < 3 * HOUR:
                    v = lambda k: (p.get(k) or {}).get('value')
                    nws = {'station': sid, 't': t, 'tempF': c2f(v('temperature')), 'via': 'nws', 'rh': v('relativeHumidity'),
                           'dewF': c2f(v('dewpoint')), 'windMph': (v('windSpeed') or 0) / 1.609344,
                           'gustMph': v('windGust') / 1.609344 if v('windGust') is not None else None, 'dir': v('windDirection'),
                           'visMi': v('visibility') / 1609.344 if v('visibility') is not None else None,
                           'inHg': v('barometricPressure') / 3386.389 if v('barometricPressure') is not None else None,
                           'windChillF': c2f(v('windChill')), 'heatIndexF': c2f(v('heatIndex'))}
                break
        if nws:
            break
    iem = None
    ids = [s[1:] if re.fullmatch(r'K[A-Z0-9]{3}', s) else s for s in stations[:4]]
    try:
        j = get_json('https://mesonet.agron.iastate.edu/api/1/currents.json?' + '&'.join('station=' + i for i in ids), headers={'User-Agent': UA['User-Agent']})
        rows = j.get('data', [])
        for sid, i in zip(stations[:4], ids):
            d = next((r for r in rows if r['station'] == i and str(r.get('network', '')).endswith('_ASOS')), None)
            if d and d.get('tmpf') is not None and d.get('utc_valid'):
                t = dt.datetime.fromisoformat(d['utc_valid'].replace('Z', '+00:00')).timestamp()
                if now - t < 2 * HOUR:
                    f = lambda k: None if d.get(k) in (None, '') else float(d[k])
                    kt = f('sknt') or 0
                    iem = {'station': sid, 't': t, 'tempF': f('tmpf'), 'via': 'iem', 'rh': f('relh'), 'dewF': f('dwpf'),
                           'windMph': kt * 1.852 / 1.609344, 'gustMph': f('gust') * 1.852 / 1.609344 if f('gust') is not None else None,
                           'dir': f('drct') if kt else None, 'visMi': f('vsby'), 'inHg': f('alti'), 'windChillF': None, 'heatIndexF': None}
                    break
    except Exception:
        pass
    if nws and iem:
        return iem if iem['t'] > nws['t'] else nws
    return nws or iem


def feels_like(T, rh, v):
    if T is None:
        return None
    if T <= 50 and v is not None and v >= 3:
        return 35.74 + 0.6215 * T - 35.75 * v ** 0.16 + 0.4275 * T * v ** 0.16
    if T >= 80 and rh is not None:
        hi = 0.5 * (T + 61 + (T - 68) * 1.2 + rh * 0.094)
        if (hi + T) / 2 >= 80:
            hi = (-42.379 + 2.04901523 * T + 10.14333127 * rh - 0.22475541 * T * rh - 0.00683783 * T * T
                  - 0.05481717 * rh * rh + 0.00122874 * T * T * rh + 0.00085282 * T * rh * rh - 0.00000199 * T * T * rh * rh)
            if rh < 13 and 80 <= T <= 112:
                hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(T - 95)) / 17)
            elif rh > 85 and 80 <= T <= 87:
                hi += ((rh - 85) / 10) * ((87 - T) / 5)
        return hi
    return T


DIRS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']


def tile_expectations(o):
    """What each tile should read for the reading shown as "now"."""
    out = {}
    feels = o['windChillF'] if o.get('windChillF') is not None else o['heatIndexF'] if o.get('heatIndexF') is not None else feels_like(o['tempF'], o['rh'], o['windMph'])
    out['Feels like'] = f"{jsround(feels)}°"
    if o['rh'] is not None:
        out['Humidity'] = f"{jsround(o['rh'])}%"
    if o['dewF'] is not None:
        out['Dew point'] = f"dew point is {jsround(o['dewF'])}°"
    out['Wind'] = f"{jsround(o['windMph'])} mph"
    out['Gusts'] = f"{jsround(o['gustMph'])} mph" if o['gustMph'] is not None else '—'
    if o['dir'] is not None:
        out['Direction'] = f"{jsround(o['dir'])}° {DIRS[int(math.floor(o['dir'] / 22.5 + 0.5)) % 16]}"
    else:
        out['Direction'] = 'Variable' if o['windMph'] >= 1 else 'Calm'
    if o['visMi'] is not None:
        v = o['visMi']
        out['Visibility'] = '10+ mi' if v >= 9.95 else (f'{v:.1f} mi' if v < 1 else f'{jsround(v)} mi')
    if o['inHg'] is not None:
        out['Pressure'] = f"{o['inHg']:.2f} inHg"
    return out


def ext_model(lat, lon, tz):
    base = (f'https://api.open-meteo.com/v1/forecast?latitude={lat:.4f}&longitude={lon:.4f}&forecast_days=12&timezone={tz}'
            '&timeformat=unixtime&temperature_unit=fahrenheit&hourly=temperature_2m,precipitation_probability')
    gfs = re.search(r'/gridpoints/(AER|ALU|AFG|AJK|AFC|HFO|SJU|GUM)/', get_json(f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}')['properties']['forecast'])
    if not gfs:
        try:
            j = get_json(base + '&models=ncep_nbm_conus', headers={'User-Agent': 'x'})
            j['_model'] = 'NBM'
            return j
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
    j = get_json(base + '&models=gfs_seamless', headers={'User-Agent': 'x'})
    j['_model'] = 'GFS'
    return j


def ext_days(j, zone):
    """NWS-style days from hourly values: high 7 AM-7 PM, low over the following night 7 PM-8 AM,
    chance = highest hourly chance; only whole days (20+ hours, 10+ daytime hours)."""
    acc = {}
    def A(d):
        return acc.setdefault(d, {'n': 0, 'hi': [], 'lo': [], 'pop': 0})
    for ts, v, pop in zip(j['hourly']['time'], j['hourly']['temperature_2m'], j['hourly']['precipitation_probability']):
        if v is None:
            continue
        t = dt.datetime.fromtimestamp(ts, zone)
        d, hh = t.date().isoformat(), t.hour
        a = A(d)
        a['n'] += 1
        if 7 <= hh <= 19:
            a['hi'].append(v)
        if hh >= 19:
            a['lo'].append(v)
        elif hh <= 8:
            A((t.date() - dt.timedelta(days=1)).isoformat())['lo'].append(v)
        if pop is not None:
            a['pop'] = max(a['pop'], pop)
    return {d: {'hi': max(a['hi']), 'lo': min(a['lo']) if len(a['lo']) >= 12 else None, 'pop': a['pop']}
            for d, a in acc.items() if a['n'] >= 20 and len(a['hi']) >= 10}


def nws_rows(periods, zone):
    """Day period + the night after it, the way weather.gov's 7-day forecast pairs them."""
    rows, i = [], 0
    if periods and not periods[0]['isDaytime']:
        st = dt.datetime.fromisoformat(periods[0]['startTime']).astimezone(zone)
        rows.append({'date': st.date().isoformat(), 'day': None, 'night': periods[0], 'early': st.hour < 6})
        i = 1
    while i < len(periods):
        p = periods[i]
        d = dt.datetime.fromisoformat(p['startTime']).astimezone(zone).date().isoformat()
        if p['isDaytime']:
            r = {'date': d, 'day': p, 'night': None}
            if i + 1 < len(periods) and not periods[i + 1]['isDaytime']:
                r['night'] = periods[i + 1]
                i += 1
            rows.append(r)
        else:
            rows.append({'date': d, 'day': None, 'night': p})
        i += 1
    if len(rows) > 1 and rows[0].get('early') and rows[1]['date'] == rows[0]['date']:
        rows[1]['pre'] = rows[0]['night']
        rows.pop(0)
    out = {}
    for r in rows:
        hi = r['day']['temperature'] if r['day'] else None
        lo = r['night']['temperature'] if r['night'] else None
        if r.get('pre') is not None and (lo is None or r['pre']['temperature'] < lo):
            lo = r['pre']['temperature']
        main = r['day'] or r['night']
        pop = (main.get('probabilityOfPrecipitation') or {}).get('value') or 0
        out[r['date']] = {'hi': hi, 'lo': lo, 'pop': pop, 'dayOnly': r['day'] is not None and r['night'] is None}
    return out


def jsround(x):
    """Round half up, the way the app's Math.round does (Python's round() goes to even)."""
    return None if x is None else int(math.floor(x + 0.5))


def round10(p):
    return int(math.floor(p / 10 + 0.5) * 10) if p is not None else 0


# ---------------------------------------------------------------- sun + moon, independently
def sun_events(lat, lon, zone, day):
    from astral import Observer
    from astral.sun import sunrise, sunset
    obs = Observer(latitude=lat, longitude=lon)
    out = {}
    for k, fn in (('rise', sunrise), ('set', sunset)):
        try:
            out[k] = fn(obs, date=day, tzinfo=zone)
        except Exception:
            out[k] = None
    return out


def moon_phases(after_utc, n=2):
    import ephem
    names = [(ephem.next_new_moon, 'New Moon'), (ephem.next_first_quarter_moon, 'First Quarter'),
             (ephem.next_full_moon, 'Full Moon'), (ephem.next_last_quarter_moon, 'Last Quarter')]
    evs = []
    for fn, name in names:
        d = fn(ephem.Date(after_utc))
        evs.append((ephem.Date(d).datetime().replace(tzinfo=dt.timezone.utc), name))
    evs.sort()
    return evs[:n]


# ---------------------------------------------------------------- app side
EXTRACT = r"""() => {
  const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
  const txt = el => el ? el.textContent.trim() : null;
  const tiles = {};
  for (const t of qa('.tile')) tiles[txt(t.querySelector('.card-h'))] = t.innerText;
  const sunTile = qa('.tile.wide').find(t => /Sunrise|Sunset/.test(txt(t.querySelector('.card-h')) || ''));
  return {
    state: txt(q('.state')),
    place: txt(q('.hero-place')), temp: txt(q('.hero-temp')), cond: txt(q('.hero-cond')), hilo: txt(q('.hero-hilo')),
    src: qa('.hero-src').map(txt).pop() || null,
    hours: qa('.hours .hr:not(.sun)').map(h => ({ t: txt(h.querySelector('.t')), v: txt(h.querySelector('.v')), p: txt(h.querySelector('.p')) })),
    days: qa('#main .day').map(d => ({ date: d.dataset.day, label: txt(d.querySelector('.dn')), lo: txt(d.querySelector('.lo')), hi: txt(d.querySelector('.hi')), p: txt(d.querySelector('.di .p')), est: d.querySelector('.lo.est') != null })),
    extTag: txt(q('.xdiv .tag')),
    tiles,
    sun: sunTile ? { head: txt(sunTile.querySelector('.card-h')), when: txt(sunTile.querySelector('.muted')), kv: [...sunTile.querySelectorAll('.kv dd')].map(txt) } : null,
    moon: (() => { const t = qa('.tile').find(x => /Moon/.test(txt(x.querySelector('.card-h')) || '')); return t ? t.querySelector('.small').innerText.split('\n') : null; })(),
    radar: (() => { const i = q('.radar img'); return i ? { ok: i.complete && i.naturalWidth > 0, src: i.src } : null; })(),
    afd: !!q('.afd .afd-body'),
    sources: txt(q('.sources dl')),
    notices: qa('.notice').map(txt),
    about: !!q('.about'),
    bad: (document.body.innerText.match(/\bNaN\b|\bundefined\b|null°|Infinity/g) || []),
  };
}"""

TIME_RE = re.compile(r'(\d{1,2}):(\d{2})\s*([AP]M)')


def parse_clock(s, zone, base_date):
    m = TIME_RE.search(s or '')
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    h = h % 12 + (12 if ap == 'PM' else 0)
    return dt.datetime(base_date.year, base_date.month, base_date.day, h, mi, tzinfo=zone)


def num(s):
    m = re.search(r'-?\d+', s or '')
    return int(m.group()) if m else None


async def check_place(browser, base, place, sem, results):
    name, region, lat, lon = place
    res = {'place': f'{name}, {region}', 'fail': [], 'warn': [], 'info': []}
    results.append(res)
    async with sem:
        t_start = time.time()
        # ---- sources, independently
        try:
            pt = await asyncio.to_thread(get_json, f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}')
            pp = pt['properties']
            zone = ZoneInfo(pp['timeZone'])
            stations_j = await asyncio.to_thread(get_json, pp['observationStations'] + '?limit=6')
            stations = [f['properties']['stationIdentifier'] for f in stations_j['features']]
            fc = await asyncio.to_thread(get_json, pp['forecast'])
            hr = await asyncio.to_thread(get_json, pp['forecastHourly'])
        except Exception as e:
            res['fail'].append(f'weather.gov unavailable for this point: {e}')
            return
        res['info'].append(f"NWS {pp['gridId']} {pp['gridX']},{pp['gridY']} · {pp['timeZone']} · radar {pp.get('radarStation')} · stations {' '.join(stations[:4])}")

        # ---- the app
        ctx = await browser.new_context(viewport={'width': 1280, 'height': 900})
        await ctx.add_init_script(f"""try {{
            localStorage.setItem('gp:places', JSON.stringify([{{id:'t', name:{json.dumps(name)}, region:{json.dumps(region)}, lat:{lat}, lon:{lon}}}]));
            localStorage.setItem('gp:selected', JSON.stringify('t'));
            localStorage.setItem('gp:units', JSON.stringify('us'));
        }} catch (e) {{}}""")
        page = await ctx.new_page()
        errors = []
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
        page.on('pageerror', lambda e: errors.append(str(e)))
        try:
            await page.goto(base, wait_until='domcontentloaded')
            await page.wait_for_function("(document.querySelector('.hero-temp') && document.querySelector('.hero-temp').textContent.trim() !== '--') || document.querySelector('.state')", timeout=120000)
            await page.wait_for_timeout(1200)
            app = await page.evaluate(EXTRACT)
        except Exception as e:
            res['fail'].append(f'app did not finish loading: {e}')
            await ctx.close()
            return
        # reference data for the comparisons; if this script can't reach a source, skip that comparison
        try:
            exp_now = await asyncio.to_thread(expected_now, stations)
        except Exception as e:
            exp_now = None
            res['warn'].append(f'check could not fetch station reports itself: {e}')
        try:
            ext = await asyncio.to_thread(ext_model, lat, lon, pp['timeZone'])
        except Exception as e:
            ext = None
            res['warn'].append(f'check could not fetch the model itself: {e}')
        if app['state']:
            res['fail'].append('app shows an error: ' + app['state'])
            await ctx.close()
            return

        now_local = dt.datetime.now(zone)
        today = now_local.date()

        # ---- now
        t = num(app['temp'])
        if exp_now:
            want = jsround(exp_now['tempF'])
            st_ok = exp_now['station'] in (app['src'] or '')
            if t == want and st_ok:
                res['info'].append(f"now {t}° from {exp_now['station']} ({exp_now['via']}) ✓")
            elif abs((t or 999) - want) <= 1 or not st_ok:
                res['warn'].append(f"now: app {app['temp']} [{app['src']}] vs newest report {want}° at {exp_now['station']} via {exp_now['via']} (a new report may have landed in between)")
            else:
                res['fail'].append(f"now: app {app['temp']} [{app['src']}] vs newest report {want}° at {exp_now['station']}")
        else:
            if 'forecast for this hour' not in (app['src'] or ''):
                res['fail'].append(f"no recent station report exists, but the app doesn't say it's showing the forecast: {app['src']}")
            else:
                res['warn'].append('no station report in the last 2-3 hours; app correctly falls back to the forecast')

        # ---- tiles vs the same reading
        if exp_now and exp_now['station'] in (app['src'] or '') and str(jsround(exp_now['tempF'])) in (app['temp'] or ''):
            want = tile_expectations(exp_now)
            T = app['tiles']
            got = {
                'Feels like': (re.search(r'-?\d+°', T.get('Feels like', '')) or [None])[0],
                'Humidity': (re.search(r'\d+%', T.get('Humidity', '')) or [None])[0],
                'Dew point': (re.search(r'dew point is -?\d+°', T.get('Humidity', '')) or [None])[0],
                'Visibility': (re.search(r'(10\+|\d+(\.\d)?) mi', T.get('Visibility', '')) or [None])[0],
                'Pressure': (re.search(r'\d+\.\d\d inHg', T.get('Pressure', '')) or [None])[0],
            }
            wl = (T.get('Wind', '') or '').split('\n')
            kv = {wl[i]: wl[i + 1] for i in range(len(wl) - 1) if wl[i] in ('Wind', 'Gusts', 'Direction')}
            got.update({k: kv.get(k) for k in ('Wind', 'Gusts', 'Direction')})
            off = [f"{k}: app {got.get(k)!r} vs {v!r}" for k, v in want.items() if got.get(k) != v]
            if off:
                res['fail'].append('tiles: ' + '; '.join(off))
            else:
                res['info'].append('tiles (feels like, humidity, dew point, wind, gusts, direction, visibility, pressure) match the reading ✓')

        # ---- metric units: same reading, converted
        if exp_now and exp_now['station'] in (app['src'] or '') and str(jsround(exp_now['tempF'])) in (app['temp'] or ''):
            try:
                await page.click('[data-units="metric"]')
                await page.wait_for_timeout(150)
                mt = await page.evaluate("[document.querySelector('.hero-temp').textContent, document.querySelector('.tile.wide') ? document.querySelector('.tile.wide').innerText : '']")
                wantC = f"{jsround((exp_now['tempF'] - 32) * 5 / 9)}°"
                wantW = f"{jsround(exp_now['windMph'] * 1.609344)} km/h"
                if wantC not in mt[0] or wantW not in mt[1]:
                    res['fail'].append(f"metric: app {mt[0]!r} / wind tile {mt[1].split(chr(10))[-5:]} vs {wantC}, {wantW}")
                else:
                    res['info'].append(f'metric units convert correctly ({wantC}, {wantW}) ✓')
                await page.click('[data-units="us"]')
                await page.wait_for_timeout(100)
            except Exception as e:
                res['warn'].append(f'metric check skipped: {e}')

        # ---- hourly strip vs NWS hourly
        def hourly_mismatch(hj):
            periods = hj['properties']['periods']
            start = next((i for i, p in enumerate(periods) if dt.datetime.fromisoformat(p['endTime']).timestamp() > time.time()), 0)
            out = []
            for k, h in enumerate(app['hours'][1:7], start=1):
                if start + k >= len(periods):
                    break
                p = periods[start + k]
                if num(h['v']) != p['temperature']:
                    out.append(f"{h['t']} app {h['v']} vs NWS {p['temperature']}°")
                pop = round10((p.get('probabilityOfPrecipitation') or {}).get('value') or 0)
                shown = num(h['p']) if h['p'] else 0
                if (pop >= 20 and shown != pop) or (pop < 20 and shown):
                    out.append(f"{h['t']} chance app {h['p']} vs NWS {pop}%")
            return out
        mism = hourly_mismatch(hr)
        if mism:
            versions = await nws_versions(pp['forecastHourly'], hr)
            newest = max(upd(v) for v in versions)
            match = next((v for v in versions if not hourly_mismatch(v)), None)
            if match is not None:
                if upd(match) < newest:
                    res['warn'].append(f"hourly: app shows the NWS update from {hhmm(upd(match))}; some weather.gov servers already have {hhmm(newest)} (they disagree for a while after each update)")
                else:
                    res['info'].append(f"hourly strip matches the newest NWS update ({hhmm(newest)}) ✓ (weather.gov first handed this check an older copy)")
                mism = None
        if mism:
            res['fail'].append('hourly: ' + '; '.join(mism[:4]))
        elif mism == []:
            res['info'].append(f"hourly strip matches NWS for the next {min(6, len(app['hours']) - 1)} hours ✓")

        # ---- daily rows vs NWS periods + extended model
        ext_rows = ext_days(ext, zone) if ext else {}
        if not ext:
            ext = {'_model': app['extTag'] or 'NBM'}
        if app['extTag'] and app['extTag'] != ext['_model']:
            res['fail'].append(f"extended outlook labeled {app['extTag']} but the source is {ext['_model']}")

        def daily_cmp(fj):
            exp_rows = nws_rows(fj['properties']['periods'], zone)
            bad = []
            n_nws = n_ext = 0
            for r in app['days']:
                d = r['date']
                if d < today.isoformat():
                    bad.append(f'{d} is in the past but still listed')
                    continue
                if d in exp_rows:
                    n_nws += 1
                    e = exp_rows[d]
                    if e['hi'] is not None and num(r['hi']) != e['hi'] and d != today.isoformat():
                        bad.append(f"{r['label']} high app {r['hi']} vs NWS {e['hi']}°")
                    if e['lo'] is not None and num(r['lo']) != e['lo']:
                        bad.append(f"{r['label']} low app {r['lo']} vs NWS {e['lo']}°")
                    if e['lo'] is None and r['lo']:
                        x = ext_rows.get(d)
                        if not r['est']:
                            bad.append(f"{r['label']} low {r['lo']} not marked as a model estimate")
                        elif x and x['lo'] is not None and num(r['lo']) != jsround(x['lo']):
                            bad.append(f"{r['label']} model low app {r['lo']} vs {ext['_model']} night min {jsround(x['lo'])}°")
                    pop = round10(e['pop'])
                    shown = num(r['p']) if r['p'] else 0
                    if (pop >= 20 and shown != pop) or (pop < 20 and shown):
                        bad.append(f"{r['label']} chance app {r['p']} vs NWS {pop}%")
                elif not ext_rows:
                    n_ext += 1
                elif d in ext_rows:
                    n_ext += 1
                    x = ext_rows[d]
                    if num(r['hi']) != jsround(x['hi']):
                        bad.append(f"{r['label']} {ext['_model']} high app {r['hi']} vs {jsround(x['hi'])}°")
                    if x['lo'] is not None and num(r['lo']) != jsround(x['lo']):
                        bad.append(f"{r['label']} {ext['_model']} low app {r['lo']} vs {jsround(x['lo'])}°")
                else:
                    bad.append(f"{r['label']} ({d}) isn't in either source")
            return bad, n_nws, n_ext

        bad, n_nws, n_ext = daily_cmp(fc)
        if bad:
            versions = await nws_versions(pp['forecast'], fc)
            newest = max(upd(v) for v in versions)
            tried = sorted(((daily_cmp(v), v) for v in versions), key=lambda t: len(t[0][0]))
            (bad2, n2, e2), match = tried[0]
            if not bad2:
                bad, n_nws, n_ext = bad2, n2, e2
                if upd(match) < newest:
                    res['warn'].append(f"daily: app shows the NWS update from {hhmm(upd(match))}; some weather.gov servers already have {hhmm(newest)} (they disagree for a while after each update)")
        if bad:
            res['fail'].append('daily: ' + '; '.join(bad[:5]))
        else:
            res['info'].append(f"daily: {n_nws} NWS days + {n_ext} {ext['_model']} days match ✓")
        if n_nws < 6:
            res['fail'].append(f'only {n_nws} official NWS days shown')
        if n_ext < 2:
            res['warn'].append(f'only {n_ext} extended-outlook days shown')

        # ---- sunrise/sunset vs astral
        if app['sun']:
            kv = app['sun']['kv']
            day_shown = today + dt.timedelta(days=1) if 'tomorrow' in (app['sun']['when'] or '') else today
            ev = sun_events(lat, lon, zone, day_shown)
            diffs = []
            for label, val, key in (('sunrise', kv[0], 'rise'), ('sunset', kv[1], 'set')):
                got = parse_clock(val, zone, day_shown)
                want = ev.get(key)
                if got and want:
                    diffs.append(abs((got - want.replace(second=0, microsecond=0)).total_seconds()) / 60)
            if diffs and max(diffs) > 2.5:
                res['fail'].append(f"sun times off by {max(diffs):.0f} min: app {kv[:2]} vs astral {[e.strftime('%I:%M %p') if e else None for e in ev.values()]}")
            elif diffs:
                res['info'].append(f'sunrise/sunset within {max(diffs):.0f} min of astral ✓')
        else:
            ev = sun_events(lat, lon, zone, today)
            if ev['rise'] and ev['set']:
                res['fail'].append('sun tile missing although the sun rises and sets today')
            else:
                res['info'].append('no sunrise/sunset today (polar) ✓')

        # ---- moon phases vs ephem
        if app['moon']:
            want = moon_phases(dt.datetime.now(dt.timezone.utc))
            okm = True
            for line, (when, nm) in zip([l for l in app['moon'] if ':' in l][:2], want):
                m = re.match(r'(.+?): (\w{3}) (\d{1,2}), (.+)', line.strip())
                if not m or m.group(1) != nm:
                    okm = False
                    res['fail'].append(f'moon: app "{line}" vs ephem {nm} {when.astimezone(zone):%b %d %I:%M %p}')
                    continue
                local = when.astimezone(zone)
                got = parse_clock(m.group(4), zone, local.date())
                if not got or local.strftime('%b') != m.group(2) or local.day != int(m.group(3)) or abs((got - local.replace(second=0, microsecond=0)).total_seconds()) > 180:
                    okm = False
                    res['fail'].append(f'moon: app "{line}" vs ephem {local:%b %d %I:%M %p}')
            if okm:
                res['info'].append('moon phase times within 3 min of ephem ✓')

        # ---- the rest of the page
        if not pp.get('radarStation'):
            if app['radar']:
                res['fail'].append('radar card shown although no NWS radar covers this point')
            else:
                res['info'].append('no NWS radar covers this point; radar card hidden ✓')
        elif not app['radar'] or not app['radar']['ok']:
            res['fail'].append(f"radar loop didn't load: {app['radar']}")
        if not app['afd']:
            res['warn'].append("forecaster's discussion missing")
        if app['bad']:
            res['fail'].append('bad text on screen: ' + ', '.join(sorted(set(app['bad']))))
        for n in app['notices']:
            res['warn'].append('notice shown: ' + n)

        # ---- day sheet: charts, icon row, scrub readout, page lock
        try:
            await page.click('#main .day >> nth=1')
            await page.wait_for_selector('#c1 svg', timeout=8000)
            locked = await page.evaluate("document.documentElement.classList.contains('locked')")
            icons = await page.evaluate("document.querySelectorAll('#iconrow span').length")
            box = await page.query_selector('#c1')
            bb = await box.bounding_box()
            await page.mouse.move(bb['x'] + bb['width'] * 0.45, bb['y'] + bb['height'] * 0.5)
            await page.wait_for_timeout(150)
            ro = await page.evaluate("(() => { const r = document.querySelector('#dhead .ro'); return r ? r.innerText : null; })()")
            await page.mouse.move(bb['x'] + bb['width'] * 0.45, bb['y'] - 120)
            await page.wait_for_timeout(100)
            cleared = await page.evaluate("!document.querySelector('#dhead .ro')")
            y0 = await page.evaluate('window.scrollY')
            await page.mouse.wheel(0, 600)
            await page.wait_for_timeout(200)
            y1 = await page.evaluate('window.scrollY')
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(100)
            unlocked = not await page.evaluate("document.documentElement.classList.contains('locked')")
            probs = []
            if not locked or not unlocked:
                probs.append('page lock did not toggle')
            if y1 != y0:
                probs.append('page scrolled behind the sheet')
            if icons < 8:
                probs.append(f'only {icons} icons above the chart')
            if not ro or not re.search(r'\d{1,2}:\d{2}\s*[AP]M', ro) or not re.search(r'-?\d+°', ro):
                probs.append(f'scrub readout missing or malformed: {ro!r}')
            if not cleared:
                probs.append('readout stayed after the pointer left')
            if probs:
                res['fail'].append('day sheet: ' + '; '.join(probs))
            else:
                res['info'].append(f'day sheet: charts, {icons} icons, scrub readout "{" ".join(ro.split())}" ✓')
        except Exception as e:
            res['fail'].append(f'day sheet check failed: {e}')

        # network hiccups (a source briefly failing, then retried) are logged by the browser but aren't bugs
        net = [e for e in errors if 'Failed to load resource' in e or 'CORS policy' in e or 'net::' in e]
        js = [e for e in errors if e not in net and 'favicon' not in e]
        if js:
            res['fail'].append('script errors: ' + ' | '.join(e[:200] for e in js[:3]))
        if net:
            res['warn'].append(f'{len(net)} network request(s) failed and were retried, e.g. ' + net[0][:140])
        res['info'].append(f'{time.time() - t_start:.0f}s')
        await ctx.close()
        st = 'FAIL' if res['fail'] else 'WARN' if res['warn'] else 'PASS'
        print(f"[{st}] {res['place']} ({time.time() - t_start:.0f}s)", flush=True)


def inside(pt, ring):
    x, y, ok = pt[0], pt[1], False
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            ok = not ok
    return ok


async def check_alerts(browser, base, results):
    """Find live NWS alerts somewhere in the U.S. and make sure the app shows them for a point inside."""
    res = {'place': 'Active alerts', 'fail': [], 'warn': [], 'info': []}
    results.append(res)
    try:
        j = await asyncio.to_thread(get_json, 'https://api.weather.gov/alerts/active?status=actual&message_type=alert')
    except Exception as e:
        res['warn'].append(f'could not list alerts: {e}')
        return
    picks = []
    for f in j.get('features', []):
        g = f.get('geometry')
        if not g or g.get('type') != 'Polygon':
            continue
        ring = g['coordinates'][0]
        cx, cy = sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
        if inside((cx, cy), ring):
            picks.append((f['properties'], cy, cx))
        if len(picks) >= 2:
            break
    if not picks:
        res['info'].append('no polygon alerts active right now; nothing to check')
        return
    for props, lat, lon in picks:
        ctx = await browser.new_context(viewport={'width': 1280, 'height': 900})
        await ctx.add_init_script(f"localStorage.setItem('gp:places', JSON.stringify([{{id:'a', name:'Alert test', region:'', lat:{lat:.4f}, lon:{lon:.4f}}}])); localStorage.setItem('gp:selected', JSON.stringify('a'));")
        page = await ctx.new_page()
        try:
            await page.goto(base)
            await page.wait_for_function("(document.querySelector('.hero-temp') && document.querySelector('.hero-temp').textContent.trim() !== '--') || document.querySelector('.state')", timeout=120000)
            titles = await page.evaluate("[...document.querySelectorAll('.alert-title')].map(e => e.textContent.trim())")
            if props['event'] in titles:
                await page.click(f'.alert-btn >> nth={titles.index(props["event"])}')
                body = await page.evaluate("(document.querySelector('.alert-body') || {}).innerText || ''")
                if body.strip():
                    res['info'].append(f"{props['event']} at {lat:.2f},{lon:.2f} ({props.get('senderName')}) shown and opens ✓")
                else:
                    res['fail'].append(f"{props['event']} shown but its text didn't open")
            else:
                res['fail'].append(f"{props['event']} active at {lat:.2f},{lon:.2f} but the app shows {titles}")
        except Exception as e:
            res['fail'].append(f'alert check failed: {e}')
        await ctx.close()


async def check_offline(browser, base, results):
    """Open once online, then go offline: the app should still open with the saved forecast."""
    res = {'place': 'Offline use', 'fail': [], 'warn': [], 'info': []}
    results.append(res)
    ctx = await browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    await ctx.add_init_script("if (!localStorage.getItem('gp:places')) { localStorage.setItem('gp:places', JSON.stringify([{id:'northfield-mn', name:'Northfield', region:'MN', lat:44.4620, lon:-93.1830}])); }")
    page = await ctx.new_page()
    try:
        await page.goto(base)
        await page.wait_for_function("document.querySelector('.hero-temp') && document.querySelector('.hero-temp').textContent.trim() !== '--'", timeout=120000)
        await page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.controller !== undefined", timeout=10000)
        await page.evaluate("navigator.serviceWorker.ready")
        await page.reload()
        await page.wait_for_timeout(1500)
        online_temp = await page.evaluate("document.querySelector('.hero-temp').textContent")
        await ctx.set_offline(True)
        await page.reload()
        await page.wait_for_function("document.querySelector('.hero-temp') && document.querySelector('.hero-temp').textContent.trim() !== '--'", timeout=30000)
        await page.wait_for_timeout(2500)
        st = await page.evaluate("({ temp: document.querySelector('.hero-temp').textContent, bar: document.querySelector('.updated').textContent, notice: [...document.querySelectorAll('.notice')].map(n => n.textContent).join(' | '), radar: !!document.querySelector('.radar.noimg'), days: document.querySelectorAll('#main .day').length })")
        probs = []
        if st['temp'] != online_temp:
            probs.append(f"temp changed offline {online_temp} -> {st['temp']}")
        if 'Offline' not in st['bar']:
            probs.append(f"bar doesn't say offline: {st['bar']!r}")
        if 'offline' not in st['notice'].lower():
            probs.append(f"no offline notice: {st['notice']!r}")
        if not st['radar']:
            probs.append('radar placeholder not shown')
        if st['days'] < 7:
            probs.append(f"only {st['days']} days offline")
        if probs:
            res['fail'].append('; '.join(probs))
        else:
            res['info'].append(f"opens offline from the saved copy: {st['temp']}, {st['days']} days, '{st['bar']}', radar placeholder ✓")
    except Exception as e:
        res['fail'].append(f'offline check failed: {e}')
    await ctx.set_offline(False)
    await ctx.close()


async def check_install(browser, base, results, pw):
    """The Home Screen guide: the tip on phones, the guide for each kind of device, its addresses
    (/install and #install), the one-tap Install button where the browser offers one, the installed
    state, and that the service worker leaves other pages on the site alone."""
    res = {'place': 'Home Screen guide', 'fail': [], 'warn': [], 'info': []}
    results.append(res)
    root = base[:base.rfind('/') + 1]
    seed = "if (!localStorage.getItem('gp:places')) { localStorage.setItem('gp:places', JSON.stringify([{id:'northfield-mn', name:'Northfield', region:'MN', lat:44.4620, lon:-93.1830}])); }"
    loaded = "document.querySelector('.hero-temp') && document.querySelector('.hero-temp').textContent.trim() !== '--'"
    guide = "(() => { const s = document.getElementById('installSheet'); if (!s || s.hidden) return null; return { title: s.querySelector('.ig-title').textContent, here: (s.querySelector('.ig-h') || {}).textContent, steps: s.querySelectorAll('.ig-sec .steps')[0] ? s.querySelectorAll('.ig-sec .steps')[0].children.length : 0, text: s.innerText, others: s.querySelectorAll('details').length, go: !!s.querySelector('[data-install-go]'), locked: document.documentElement.classList.contains('locked'), url: location.pathname + location.search + location.hash }; })()"

    async def ctx_for(device=None, init=''):
        opts = dict(pw.devices[device]) if device else {'viewport': {'width': 1280, 'height': 900}}
        opts.pop('default_browser_type', None)
        ctx = await browser.new_context(**opts)
        await ctx.add_init_script(seed + init)
        return ctx

    # iPhone, Safari
    ctx = await ctx_for('iPhone 13')
    page = await ctx.new_page()
    try:
        await page.goto(base)
        await page.wait_for_function(loaded, timeout=120000)
        tip = await page.evaluate("(() => { const t = document.querySelector('.tip'); return t && { text: t.innerText, how: !!t.querySelector('[data-install]') }; })()")
        if not tip or 'Home Screen' not in tip['text'] or not tip['how']:
            res['fail'].append(f'iPhone: no Home Screen tip ({tip})')
        else:
            await page.click('.tip [data-install]')
            g = await page.evaluate(guide)
            probs = []
            if not g:
                probs.append("guide didn't open from the tip")
            else:
                if g['title'] != 'Add Gridpoint to your Home Screen': probs.append(f"title {g['title']!r}")
                if g['here'] != 'On this iPhone': probs.append(f"section {g['here']!r}")
                if g['steps'] != 3 or 'Add to Home Screen' not in g['text'] or 'Open as Web App' not in g['text']: probs.append(f"steps {g['steps']}")
                if 'separate from Safari' not in g['text']: probs.append('no note about places being kept separately')
                if not g['locked']: probs.append("page behind the guide isn't locked")
                if g['others'] != 3: probs.append(f"{g['others']} other-device sections")
            await page.keyboard.press('Escape')
            after = await page.evaluate("({ open: !document.getElementById('installSheet').hidden, locked: document.documentElement.classList.contains('locked'), tip: !!document.querySelector('.tip'), about: (document.querySelector('.about-main [data-install]') || {}).textContent || '' })")
            if after['open'] or after['locked']: probs.append("Escape didn't close the guide")
            if after['tip']: probs.append('tip still shown after opening the guide')
            if 'Add Gridpoint to your Home Screen' not in after['about']: probs.append(f"About has no guide button ({after['about']!r})")
            if probs:
                res['fail'].append('iPhone: ' + '; '.join(probs))
            else:
                res['info'].append('iPhone: tip → guide (3 Safari steps, offline note), Escape closes, tip stays dismissed, About button ✓')
        # the addresses people share
        for path in ('#install', 'install.html'):
            p2 = await ctx.new_page()
            await p2.goto(base + path if path.startswith('#') else root + path)
            try:
                await p2.wait_for_function("!document.getElementById('installSheet').hidden", timeout=30000)
                g = await p2.evaluate(guide)
                if 'install' in g['url'].lower():
                    res['fail'].append(f'{path}: guide opened but the address still reads {g["url"]!r} (the Home Screen icon would open the guide)')
                else:
                    res['info'].append(f'{path}: opens the guide, address cleaned to {g["url"]!r} ✓')
            except Exception as e:
                res['fail'].append(f"{path}: guide didn't open ({str(e).splitlines()[0]})")
            await p2.close()
        # other pages on the site aren't swapped for the app by the service worker
        p3 = await ctx.new_page()
        try:
            await p3.goto(root + 'check.py')
            hijacked = await p3.evaluate("!!document.querySelector('.hero')")
            (res['fail'] if hijacked else res['info']).append('check.py: ' + ('the service worker served the app instead' if hijacked else 'loads as itself, not the app ✓'))
        except Exception as e:
            res['info'].append('check.py: downloaded rather than shown, not the app ✓')
        await p3.close()
    except Exception as e:
        res['fail'].append(f'iPhone guide check failed: {e}')
    await ctx.close()

    # already installed (opened from the Home Screen)
    ctx = await ctx_for('iPhone 13', "Object.defineProperty(navigator, 'standalone', { get: () => true });")
    page = await ctx.new_page()
    try:
        await page.goto(base)
        await page.wait_for_function(loaded, timeout=120000)
        st = await page.evaluate("({ tip: !!document.querySelector('.tip'), btn: document.querySelectorAll('[data-install]').length })")
        await page.goto(base + '#install')
        await page.wait_for_function("!document.getElementById('installSheet').hidden", timeout=30000)
        g = await page.evaluate(guide)
        if st['tip'] or st['btn'] or g['title'] != 'Gridpoint is on this device' or g['others'] != 4:
            res['fail'].append(f"installed app: tip {st['tip']}, buttons {st['btn']}, guide {g['title']!r}, {g['others']} device sections")
        else:
            res['info'].append('installed app: no tip or install button; guide says it’s installed and shows all 4 devices ✓')
    except Exception as e:
        res['fail'].append(f'installed-app check failed: {e}')
    await ctx.close()

    # Android Chrome, where the browser offers one-tap install
    ctx = await ctx_for('Pixel 7')
    page = await ctx.new_page()
    try:
        await page.goto(base)
        await page.wait_for_function(loaded, timeout=120000)
        pre = await page.evaluate("document.querySelector('.tip') && document.querySelector('.tip').innerText")
        fired = await page.evaluate("""() => { const e = new Event('beforeinstallprompt', { cancelable: true });
            e.prompt = () => { window.__prompted = (window.__prompted || 0) + 1; return Promise.resolve(); };
            e.userChoice = Promise.resolve({ outcome: 'accepted', platform: 'web' });
            window.dispatchEvent(e); return e.defaultPrevented; }""")
        await page.wait_for_selector('.tip [data-install-go]', timeout=5000)
        await page.click('.tip [data-install-go]')
        await page.wait_for_function("!document.querySelector('.tip')", timeout=5000)
        n = await page.evaluate('window.__prompted || 0')
        about_btn = await page.evaluate("document.querySelectorAll('[data-install]').length")
        await page.evaluate("location.hash = 'install'")
        await page.wait_for_function("!document.getElementById('installSheet').hidden", timeout=10000)
        g = await page.evaluate(guide)
        probs = []
        if not pre or 'Home Screen' not in pre: probs.append(f'no tip before the browser offered install ({pre!r})')
        if not fired: probs.append("the browser's own install banner wasn't held back")
        if n != 1: probs.append(f'install prompt shown {n} times')
        if about_btn: probs.append('About still offers to install after installing')
        if g['title'] != 'Gridpoint is on this device': probs.append(f"after installing, guide says {g['title']!r}")
        if probs:
            res['fail'].append('Android: ' + '; '.join(probs))
        else:
            res['info'].append('Android: tip → one-tap Install (browser prompt once), then guide shows it’s installed ✓')
    except Exception as e:
        res['fail'].append(f'Android check failed: {e}')
    await ctx.close()

    # a computer, plus a radar image that won't load
    ctx = await ctx_for()
    await ctx.route('**/ridge/**', lambda r: r.abort())
    page = await ctx.new_page()
    try:
        await page.goto(base)
        await page.wait_for_function(loaded, timeout=120000)
        tip = await page.evaluate("!!document.querySelector('.tip')")
        label = await page.evaluate("(document.querySelector('.sidebar [data-install]') || {}).textContent || ''")
        await page.click('.sidebar [data-install]')
        g = await page.evaluate(guide)
        await page.click('[data-close-guide]')
        closed = await page.evaluate("document.getElementById('installSheet').hidden")
        await page.evaluate("document.querySelector('.radar') && document.querySelector('.radar').scrollIntoView()")
        await page.wait_for_function("document.querySelector('.radar.noimg')", timeout=20000)
        msg = await page.evaluate("document.querySelector('.radar .offmsg').textContent")
        probs = []
        if tip: probs.append('phone tip shown on a computer')
        if 'Install Gridpoint as an app' not in label: probs.append(f'sidebar button {label!r}')
        if not g or g['here'] != 'On this computer' or g['steps'] != 2: probs.append(f'guide {g and (g["here"], g["steps"])}')
        if not closed: probs.append("close button didn't close the guide")
        if 'isn’t loading' not in msg: probs.append(f'radar failure says {msg!r}')
        if probs:
            res['fail'].append('Computer: ' + '; '.join(probs))
        else:
            res['info'].append('Computer: no tip; sidebar button → guide (2 steps), closes; radar failure explained ✓')
    except Exception as e:
        res['fail'].append(f'computer check failed: {e}')
    await ctx.close()


async def check_search_and_report(browser, base, results, pw):
    """Place search (instant results from the built-in list, ranking, ZIP codes, keyboard, the search box
    never being replaced while typing) and the Report a bug form (mailto with the report filled in)."""
    res = {'place': 'Search and bug reports', 'fail': [], 'warn': [], 'info': []}
    results.append(res)
    seed = ("if (!localStorage.getItem('gp:places')) { localStorage.setItem('gp:places', JSON.stringify([{id:'northfield-mn', name:'Northfield', region:'MN', lat:44.4620, lon:-93.1830}])); }"
            "localStorage.setItem('gp:tipDone', 'true');"
            "document.addEventListener('click', e => { const a = e.target.closest && e.target.closest('a[href^=\\'mailto:\\']'); if (a) { window.__mailto = a.href; e.preventDefault(); } }, true);")
    loaded = "document.querySelector('.hero-temp') && document.querySelector('.hero-temp').textContent.trim() !== '--'"
    names = "[...document.querySelectorAll('#rw-side .res .res-name')].map(x => x.textContent.trim())"
    ctx = await browser.new_context(viewport={'width': 1280, 'height': 900})
    await ctx.add_init_script(seed)
    page = await ctx.new_page()
    try:
        await page.goto(base)
        await page.wait_for_function(loaded, timeout=120000)
        box = page.locator('#q-side')
        await page.evaluate("document.getElementById('q-side').__mark = 1")
        want = {'mill': ('Millbrae, CA', 15), 'san': ('San Diego, CA', 10), 'st paul': ('Saint Paul, MN', 1), 'portland, or': ('Portland, OR', 1),
                'new york': ('New York City, NY', 1), '55057': ('Northfield, MN', 1), 'honolulu': ('Honolulu, HI', 1), 'san juan pr': ('San Juan, PR', 1)}
        probs, seen = [], []
        for q, (name, top) in want.items():
            await box.fill('')
            await box.type(q, delay=25)
            try:
                await page.wait_for_function(f"[...document.querySelectorAll('#rw-side .res .res-name')].some(x => x.textContent.trim() === {json.dumps(name)})", timeout=15000)
            except Exception:
                pass
            got = await page.evaluate(names)
            if name not in got[:top]:
                probs.append(f'{q!r}: {name} not in the top {top} ({got[:5]})')
            else:
                seen.append(f'{q} → {name}')
        same = await page.evaluate("document.activeElement && document.activeElement.id === 'q-side' && document.activeElement.__mark === 1")
        if not same:
            probs.append('the search box was replaced or lost focus while typing')
        await box.fill('')
        await box.type('xyzq', delay=25)
        await page.wait_for_timeout(600)
        empty = await page.evaluate("(document.querySelector('#rw-side .empty') || {}).textContent || ''")
        if 'No U.S. places match' not in empty:
            probs.append(f'no "no matches" message ({empty!r})')
        await box.fill('')
        await box.type('duluth', delay=25)
        await page.wait_for_function("document.querySelectorAll('#rw-side .res').length > 1", timeout=15000)
        await page.keyboard.press('ArrowDown')
        await page.keyboard.press('ArrowDown')
        pick = await page.evaluate("document.querySelector('#rw-side .res.hl .res-name').textContent.trim()")
        await page.keyboard.press('Enter')
        await page.wait_for_timeout(500)
        hero = await page.evaluate("document.querySelector('.hero-place').textContent.trim()")
        if not pick.startswith(hero):
            probs.append(f'arrow keys + Enter picked {pick!r} but the app shows {hero!r}')
        await box.click()
        await box.type('bos', delay=25)
        await page.keyboard.press('Escape')
        st = await page.evaluate("({ q: document.getElementById('q-side').value, list: !document.getElementById('pw-side').hidden })")
        if st['q'] or not st['list']:
            probs.append(f"Escape didn't clear the search ({st})")
        if probs:
            res['fail'].append('search: ' + '; '.join(probs))
        else:
            res['info'].append('search: ' + ', '.join(seen) + '; no-match message, arrow keys + Enter, Escape, box kept while typing ✓')

        # Report a bug
        await page.click('.sidebar [data-bug]')
        await page.click('[data-bug-send]')
        complained = await page.evaluate("!document.getElementById('bug-err').hidden")
        await page.fill('#bug-text', 'Test report: the search found the wrong town.')
        await page.select_option('#bug-area', 'Search')
        await page.click('[data-bug-send]')
        mail = await page.evaluate('window.__mailto || ""')
        u = urllib.parse.urlparse(mail)
        qs = urllib.parse.parse_qs(u.query)
        body = (qs.get('body') or [''])[0]
        subj = (qs.get('subject') or [''])[0]
        probs = []
        if not complained:
            probs.append('an empty report was not stopped')
        if u.scheme != 'mailto' or u.path != 'swagmasterfive@emersonxia.lol':
            probs.append(f'mail goes to {u.scheme}:{u.path}')
        if not subj.startswith('Gridpoint bug report: Test report'):
            probs.append(f'subject {subj!r}')
        if 'Test report: the search found the wrong town.' not in body or 'Part of the app: Search' not in body or 'Gridpoint build:' not in body or 'Browser:' not in body:
            probs.append(f'body missing parts: {body[:200]!r}')
        await page.keyboard.press('Escape')
        closed = await page.evaluate("document.getElementById('bugSheet').hidden && !document.documentElement.classList.contains('locked')")
        if not closed:
            probs.append("Escape didn't close the form")
        if probs:
            res['fail'].append('bug report: ' + '; '.join(probs))
        else:
            res['info'].append('bug report: empty report stopped; mail to swagmasterfive@emersonxia.lol with subject and details; closes ✓')
    except Exception as e:
        res['fail'].append(f'search/report check failed: {e}')
    await ctx.close()

    # iPhone: the places sheet keeps the same search box (and so the keyboard) while results update
    opts = dict(pw.devices['iPhone 13'])
    opts.pop('default_browser_type', None)
    ctx = await browser.new_context(**opts)
    await ctx.add_init_script(seed)
    page = await ctx.new_page()
    try:
        await page.goto(base)
        await page.wait_for_function(loaded, timeout=120000)
        await page.click('[data-open-places]')
        await page.click('#q-sheet')
        await page.evaluate("document.getElementById('q-sheet').__mark = 1")
        await page.keyboard.type('millb', delay=60)
        await page.wait_for_function("[...document.querySelectorAll('#rw-sheet .res .res-name')].some(x => x.textContent.trim() === 'Millbrae, CA')", timeout=15000)
        same = await page.evaluate("document.activeElement && document.activeElement.__mark === 1")
        await page.click('#rw-sheet .res >> nth=0')
        await page.wait_for_timeout(400)
        st = await page.evaluate("({ sheet: !document.getElementById('placesSheet').hidden, hero: document.querySelector('.hero-place').textContent.trim() })")
        if not same or st['sheet'] or st['hero'] != 'Millbrae':
            res['fail'].append(f"iPhone search: box kept {same}, sheet open {st['sheet']}, shows {st['hero']!r}")
        else:
            res['info'].append('iPhone search: same box (keyboard stays up) while typing; tapping Millbrae adds it and closes the sheet ✓')
    except Exception as e:
        res['fail'].append(f'iPhone search check failed: {e}')
    await ctx.close()


# NWS wording every language must translate on its own (no falling back to the icon): forecast phrases, then
# station-report wording (chance None). Alert and period names too.
LANG_PHRASES = [(x, 40) for x in [
    'Sunny', 'Mostly Sunny', 'Partly Sunny', 'Mostly Cloudy', 'Cloudy', 'Clear', 'Mostly Clear', 'Partly Cloudy', 'Chance Rain Showers', 'Rain Showers Likely',
    'Slight Chance Light Rain', 'Chance Light Snow', 'Snow Likely', 'Chance Rain And Snow', 'Isolated Showers And Thunderstorms', 'Scattered Showers And Thunderstorms',
    'Showers And Thunderstorms Likely', 'Patchy Fog Then Mostly Sunny', 'Areas Of Fog', 'Patchy Frost', 'Patchy Drizzle', 'Chance Freezing Rain', 'Wintry Mix Likely',
    'Blowing Snow', 'Heavy Snow', 'Mostly Sunny And Breezy', 'Sunny And Hot', 'Partly Cloudy And Windy', 'Haze', 'Areas Of Smoke', 'Slight Chance T-storms',
    'Chance Flurries', 'Freezing Spray', 'Increasing Clouds', 'Decreasing Clouds', 'Becoming Sunny', 'Gradual Clearing', 'Chance Showers Then Slight Chance Rain']] + [(x, None) for x in [
    'Fog/Mist', 'Light Rain and Fog/Mist', 'Thunderstorms and Rain', 'Unknown Precipitation', 'Light Snow', 'Heavy Rain', 'Thunderstorm in Vicinity', 'Light Freezing Rain', 'Smoke']]
LANG_ALERTS = ['Tornado Warning', 'Severe Thunderstorm Watch', 'Winter Storm Warning', 'Heat Advisory', 'Special Weather Statement', 'Red Flag Warning', 'Flood Watch',
               'Wind Advisory', 'Frost Advisory', 'Dense Fog Advisory', 'Hurricane Warning', 'Air Quality Alert']
LANG_PERIODS = ['Tonight', 'This Afternoon', 'Overnight', 'Tuesday Night', 'Thanksgiving Day']


async def check_languages(browser, base, results, pw):
    """The language packs: every word the app uses translated in every language with its {placeholders} kept;
    then each language in a phone-sized browser (no word missing, nothing wider than the screen, NWS wording
    translated), "Automatic" following the device's language, and the language picker."""
    res = {'place': 'Languages', 'fail': [], 'warn': [], 'info': []}
    results.append(res)
    root = base.rsplit('/', 1)[0] + '/' if base.endswith('.html') else base.rstrip('/') + '/'
    try:
        with urllib.request.urlopen(urllib.request.Request(root + 'index.html', headers={'User-Agent': UA['User-Agent']}), timeout=40) as r:
            html = r.read().decode('utf-8')
    except Exception as e:
        res['fail'].append(f'could not read index.html: {e}')
        return
    langs = re.findall(r"\['([A-Za-z-]+)', '[^']+', '[A-Za-z]{2,3}-[A-Z]{2}'\]", html)
    codes = [c for c in langs if c != 'en']
    used = set()
    for q in ("'", '"'):
        for f in ('\\$t', 'tk'):
            used |= {m.replace('\\' + q, q) for m in re.findall(f + r'\(' + q + r'((?:[^' + q + r'\\]|\\.)*)' + q, html)}
    if len(codes) < 13:
        res['fail'].append(f'language list in index.html looks wrong: {langs}')
    packs, probs = {}, []
    for c in codes:
        try:
            packs[c] = (await asyncio.to_thread(get_json, root + f'i18n/{c}.json', {'User-Agent': UA['User-Agent']}, 3))['strings']
        except Exception as e:
            probs.append(f'{c}: pack not loading ({e})')
    allkeys = set().union(*packs.values()) if packs else set()
    fixed = ('n:', 'c:', 'cx:', 'pn:', 'ap:', 'at:Warning', 'at:Watch', 'at:Advisory', 'at:Statement', 'at:Emergency', 'at:Alert', 'at:Outlook', 'at:Message', 'dl:', 'hs:', 'al:')
    for c, S in packs.items():
        miss = sorted((used | allkeys) - set(S))
        if miss:
            probs.append(f'{c}: {len(miss)} words missing, e.g. {miss[:3]}')
        for k, v in S.items():
            if not isinstance(v, str) or (not v.strip() and k != 'cx:then'):
                probs.append(f'{c}: empty translation for {k!r}'); continue
            if k.startswith('q:'):
                if not re.search(r'\{(x|X|xo)\}', v): probs.append(f'{c}: {k} has no {{x}}: {v!r}')
                continue
            if k.startswith(fixed):
                continue
            if sorted(set(re.findall(r'\{\w+\}', k))) != sorted(set(re.findall(r'\{\w+\}', v))):
                probs.append(f'{c}: placeholders differ in {k[:40]!r}: {v[:60]!r}')
            if v.count('<b>') != v.count('</b>') or (('<b>' in v) and '<b>' not in k):
                probs.append(f'{c}: <b> tags off in {k[:40]!r}')
    if probs:
        res['fail'].append('language packs: ' + '; '.join(probs[:8]) + (f' (+{len(probs) - 8} more)' if len(probs) > 8 else ''))
    else:
        res['info'].append(f'{len(packs)} language packs complete: {len(allkeys)} words each, placeholders and bold labels kept ✓')

    seed = ("localStorage.setItem('gp:places', JSON.stringify([{id:'northfield-mn', name:'Northfield', region:'MN', lat:44.4620, lon:-93.1830}]));"
            "localStorage.setItem('gp:tipDone', 'true');")
    loaded = "document.querySelector('.hero-temp') && document.querySelector('.hero-temp').textContent.trim() !== '--'"
    layout = r"""() => {
      const inScroller = el => { for (let p = el.parentElement; p; p = p.parentElement) { const o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'scroll') return true; } return false; };
      const spill = [];
      for (const el of document.querySelectorAll('#main *')) {
        if (el.closest('svg') || !el.clientWidth || inScroller(el)) continue;
        const cs = getComputedStyle(el);
        if (cs.overflowX === 'visible' && cs.display !== 'inline' && el.scrollWidth > el.clientWidth + 2) spill.push((el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 30));
      }
      const sun = [...document.querySelectorAll('.hr.sun .v')].filter(v => v.scrollWidth > v.clientWidth + 1).map(v => v.textContent);
      return { wide: document.documentElement.scrollWidth - document.documentElement.clientWidth, spill: [...new Set(spill)].slice(0, 4), sun };
    }"""
    phrases = json.dumps(LANG_PHRASES)
    words = r"""([P, A, N]) => { const T = window.__gpText, bad = [], latin = /^(zh|ja|ko|ru|el)/.test(document.documentElement.lang);
      const leftover = s => latin && /[A-Za-z]{3,}/.test(s.replace(/\b(NWS|NOAA|T-storms?)\b/g, ''));
      for (const [s, pop] of P) { const o = T.condText(s, '', pop); if (o === s || leftover(o)) bad.push(s + ' → ' + o); }
      for (const a of A) { const o = T.alertName(a); if (o === a || leftover(o)) bad.push(a + ' → ' + o); }
      for (const n of N) { const o = T.periodName(n); if (o === n) bad.push(n + ' → ' + o); }
      return bad; }"""
    sem = asyncio.Semaphore(4)
    per = {}

    async def one(code):
        async with sem:
            ctx = await browser.new_context(viewport={'width': 375, 'height': 812}, device_scale_factor=2, is_mobile=True, has_touch=True, locale='en-US')
            await ctx.add_init_script(seed + f"localStorage.setItem('gp:lang', JSON.stringify({json.dumps(code)}));")
            page = await ctx.new_page()
            errs = []
            page.on('pageerror', lambda e: errs.append(str(e)[:120]))
            out = []
            try:
                await page.goto(base)
                await page.wait_for_function(loaded, timeout=120000)
                await page.wait_for_timeout(800)
                got = await page.evaluate('document.documentElement.lang')
                if got != code:
                    out.append(f'page language is {got!r}')
                lay = await page.evaluate(layout)
                if lay['wide'] > 0:
                    out.append(f"page {lay['wide']}px wider than the phone screen")
                if lay['sun']:
                    out.append(f"sunrise/sunset word too wide for its hour: {lay['sun']}")
                if lay['spill']:
                    per.setdefault('spill', []).append(f"{code}: {lay['spill']}")
                bad = await page.evaluate(words, [LANG_PHRASES, LANG_ALERTS, LANG_PERIODS])
                if bad:
                    out.append(f'NWS wording left in English: {bad[:4]}')
                await page.click('#main .day >> nth=1')
                await page.wait_for_timeout(300)
                for tab in await page.locator('#daySheet [data-metric]').all():
                    await tab.click()
                    await page.wait_for_timeout(120)
                await page.keyboard.press('Escape')
                await page.wait_for_timeout(250)
                await page.click('.bar [data-lang]')
                await page.wait_for_timeout(250)
                await page.keyboard.press('Escape')
                miss = await page.evaluate('[...window.__i18nMiss]')
                if miss:
                    out.append(f'words with no translation: {miss[:5]}')
                if errs:
                    out.append(f'script errors: {errs[:2]}')
            except Exception as e:
                out.append(f'check failed: {str(e)[:160]}')
            await ctx.close()
            per[code] = out

    await asyncio.gather(*(one(c) for c in packs))
    bad = {c: v for c, v in per.items() if c != 'spill' and v}
    for c, v in bad.items():
        res['fail'].append(f'{c}: ' + '; '.join(v))
    if per.get('spill'):
        res['warn'].append('text a little wider than its box: ' + '; '.join(per['spill'][:4]))
    ok = [c for c in packs if not per.get(c)]
    if ok:
        res['info'].append(f"{', '.join(ok)}: every word translated, phone layout fits, NWS wording, alert and period names translated, no errors ✓")

    # "Automatic" follows the device's language list; the picker switches and goes back
    try:
        probs = []
        for loc, want in [('zh-TW', 'zh-Hant'), ('es-MX', 'es')]:
            ctx = await browser.new_context(viewport={'width': 1280, 'height': 900}, locale=loc)
            await ctx.add_init_script(seed)
            page = await ctx.new_page()
            await page.goto(base)
            await page.wait_for_function(loaded, timeout=120000)
            got = await page.evaluate('document.documentElement.lang')
            if got != want:
                probs.append(f'device language {loc} opened in {got!r}, not {want!r}')
            if loc == 'es-MX':
                await page.click('.bar [data-lang]')
                await page.click('#langSheet [data-set-lang="fr"]')
                await page.wait_for_function("document.documentElement.lang === 'fr'", timeout=20000)
                stored = await page.evaluate("localStorage.getItem('gp:lang')")
                if stored != '"fr"':
                    probs.append(f'choosing Français stored {stored!r}')
                if await page.evaluate("document.getElementById('langSheet').hidden"):
                    await page.click('.bar [data-lang]')
                await page.click('#langSheet [data-set-lang="auto"]')
                await page.wait_for_function("document.documentElement.lang === 'es'", timeout=20000)
                if 'Ningún lugar' not in (await page.evaluate("(async () => { const b = document.getElementById('q-side'); b.value = 'xyzq'; b.dispatchEvent(new Event('input', { bubbles: true })); await new Promise(r => setTimeout(r, 700)); return (document.querySelector('#rw-side .empty') || {}).textContent || ''; })()")):
                    probs.append('search message not in Spanish after going back to Automatic')
            await ctx.close()
        if probs:
            res['fail'].append('language choice: ' + '; '.join(probs))
        else:
            res['info'].append('Automatic follows the device (zh-TW → 繁體中文, es-MX → Español); picking Français and going back to Automatic ✓')
    except Exception as e:
        res['fail'].append(f'language choice check failed: {str(e)[:200]}')


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--only')
    ap.add_argument('--json')
    ap.add_argument('--root', default=ROOT)
    ap.add_argument('--url', help='test a live copy instead of the local files, e.g. https://gridpoint.emersonxia.lol/')
    ap.add_argument('--concurrency', type=int, default=3)
    args = ap.parse_args()
    places = PLACES
    if args.quick:
        places = [p for p in PLACES if p[0] in QUICK]
    if args.only:
        want = {s.strip().lower() for s in args.only.split(',')}
        places = [p for p in PLACES if p[0].lower() in want]

    srv = None
    if args.url:
        base = args.url
    else:
        handler = functools.partial(Quiet, directory=args.root)
        srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{srv.server_address[1]}/index.html'

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        launch = {}
        proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        if proxy:
            launch['proxy'] = {'server': proxy, 'bypass': '<-loopback>,127.0.0.1,localhost'}
        browser = await p.chromium.launch(**launch)
        sem = asyncio.Semaphore(args.concurrency)
        results = []
        await asyncio.gather(*(check_place(browser, base, pl, sem, results) for pl in places))
        if not args.only:
            await check_alerts(browser, base, results)
            await check_offline(browser, base, results)
        if not args.only or 'install' in args.only.lower():
            await check_install(browser, base, results, p)
        if not args.only or 'search' in args.only.lower():
            await check_search_and_report(browser, base, results, p)
        if not args.only or 'lang' in args.only.lower():
            await check_languages(browser, base, results, p)
        await browser.close()
    if srv:
        srv.shutdown()

    order = {pl[0] + ', ' + pl[1]: i for i, pl in enumerate(PLACES)}
    results.sort(key=lambda r: order.get(r['place'], 900 + len(r['place'])))
    nfail = 0
    for r in results:
        status = 'FAIL' if r['fail'] else 'WARN' if r['warn'] else 'PASS'
        nfail += bool(r['fail'])
        print(f"\n{status}  {r['place']}")
        for k in ('fail', 'warn', 'info'):
            for line in r[k]:
                print(f'   {k.upper():4} {line}')
    print(f"\n{len(results) - nfail}/{len(results)} places passed without failures")
    if args.json:
        json.dump(results, open(args.json, 'w'), indent=2)
    sys.exit(1 if nfail else 0)


if __name__ == '__main__':
    asyncio.run(main())
