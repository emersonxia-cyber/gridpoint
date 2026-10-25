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

Usage:
  python3 tests/check.py              # all places
  python3 tests/check.py --quick      # a 7-place sample
  python3 tests/check.py --only "Phoenix,Honolulu"
  python3 tests/check.py --json results.json

Needs: pip install playwright astral ephem   (and Playwright's Chromium)
Exit code 1 if anything FAILs; WARN lines are worth a look but can be timing (a new
station report arriving between the app's fetch and this script's).
"""
import argparse, asyncio, datetime as dt, functools, http.server, json, math, os, re, socketserver, sys, threading, time, urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
        periods = hr['properties']['periods']
        start = next((i for i, p in enumerate(periods) if dt.datetime.fromisoformat(p['endTime']).timestamp() > time.time()), 0)
        mism = []
        for k, h in enumerate(app['hours'][1:7], start=1):
            if start + k >= len(periods):
                break
            p = periods[start + k]
            if num(h['v']) != p['temperature']:
                mism.append(f"{h['t']} app {h['v']} vs NWS {p['temperature']}°")
            pop = round10((p.get('probabilityOfPrecipitation') or {}).get('value') or 0)
            shown = num(h['p']) if h['p'] else 0
            if (pop >= 20 and shown != pop) or (pop < 20 and shown):
                mism.append(f"{h['t']} chance app {h['p']} vs NWS {pop}%")
        if mism:
            res['fail'].append('hourly: ' + '; '.join(mism[:4]))
        else:
            res['info'].append(f"hourly strip matches NWS for the next {min(6, len(app['hours']) - 1)} hours ✓")

        # ---- daily rows vs NWS periods + extended model
        exp_rows = nws_rows(fc['properties']['periods'], zone)
        ext_rows = ext_days(ext, zone) if ext else {}
        if not ext:
            ext = {'_model': app['extTag'] or 'NBM'}
        if app['extTag'] and app['extTag'] != ext['_model']:
            res['fail'].append(f"extended outlook labeled {app['extTag']} but the source is {ext['_model']}")
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


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--only')
    ap.add_argument('--json')
    ap.add_argument('--root', default=ROOT)
    ap.add_argument('--concurrency', type=int, default=3)
    args = ap.parse_args()
    places = PLACES
    if args.quick:
        places = [p for p in PLACES if p[0] in QUICK]
    if args.only:
        want = {s.strip().lower() for s in args.only.split(',')}
        places = [p for p in PLACES if p[0].lower() in want]

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
        await browser.close()
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
