# Gridpoint

The official National Weather Service forecast for your exact spot, with live station readings and the forecasters' own reasoning. No ads, no tracking, no account.

Gridpoint is a single-page web app. There's no server code: each visitor's browser asks the public data sources directly, so the site is just static files.

## Where every number comes from

| On screen | Source |
|---|---|
| **Now** (temperature, feels like, humidity, wind, visibility, pressure) | The newest report from the nearest official weather station (NOAA/FAA airport stations), via the Iowa Environmental Mesonet, which relays reports within minutes, or api.weather.gov, whichever is newer. The station and time are always shown. If the newest report is over two hours old, Gridpoint shows the forecast for this hour and says so. |
| **Hourly and days 1–7** | The National Weather Service forecast for the place's 2.5 km grid square, issued by its local NWS office (api.weather.gov). |
| **Daily high/low** | The NWS pairing: a day's high (7 AM–7 PM) with the low of the night that follows. Today's high switches to the measured high once the measured high beats the forecast. |
| **Days 8–11** | NOAA's National Blend of Models (lower 48) or NOAA's GFS model (Alaska, Hawaii, Puerto Rico), via Open-Meteo, built into days the same way NWS builds them. Labeled as model guidance. |
| **Feels like** | NWS wind chill (≤ 50°F, wind ≥ 3 mph) and NWS heat index (≥ 80°F). |
| **Sunrise / sunset** | NOAA solar calculator equations, solved at each event (within about 20 seconds of the astral library). |
| **Moon phases** | Meeus, *Astronomical Algorithms*, ch. 49 (within a minute or two of the ephem library). |
| **Radar** | The NWS radar loop for the place's radar site. |
| **Forecaster's discussion** | The latest Area Forecast Discussion from the local NWS office. |

## Put it online

Any static host works. Two easy ones:

**Cloudflare Pages + GitHub (updates go live on every push):** put these files in a GitHub repository, then in Cloudflare go to Workers & Pages → Create → Pages → Connect to Git, pick the repository, leave the build command empty and the output directory as `/`, and deploy. Add a custom domain (for example `gridpoint.yourdomain.com`) under the project's Custom domains tab.

**GitHub Pages:** in the repository's Settings → Pages, deploy from the `main` branch, root folder. For a custom subdomain, add a CNAME record pointing to `<your-username>.github.io` and enter the domain in the Pages settings.

## Use it like an app, offline included

Open the site in Safari on an iPhone, tap Share → Add to Home Screen. On a Mac, use Safari's File → Add to Dock. No App Store and no developer account are needed. After the first visit, the app is saved on the device (service worker) along with each place's last forecast, so it opens without a connection and says when it's showing saved data.

## Type

The page asks for Helvetica Now first (Display for large numbers, Text for body, Micro for chart labels), then Helvetica Neue, which ships on every Mac and iPhone. Helvetica Now is a commercial Monotype typeface; to serve it to every visitor you need a Monotype web license and the webfont files.

## Checking it

`tests/check.py` loads the app in a headless browser for 27 places across every U.S. time zone (including Alaska, Hawaii and Puerto Rico) and compares what's on screen with the sources, re-derived independently: the station reading and every tile, the hourly strip, each daily high/low/chance, the extended outlook, sunrise/sunset (astral), moon phases (ephem), radar, alerts, metric units, chart scrubbing, the page lock behind sheets, and offline use.

```
pip install playwright astral ephem
python3 -m playwright install chromium
python3 tests/check.py            # all places
python3 tests/check.py --quick    # 7-place sample
```

WARN lines are worth a look but are often timing: a new station report or forecast can land between the app's request and the check's.

## Data credits

National Weather Service / NOAA (api.weather.gov, public domain) · Iowa Environmental Mesonet, Iowa State University · Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0), free for non-commercial use.
