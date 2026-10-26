# Gridpoint

The official National Weather Service forecast for your exact spot, with live station readings and the forecasters' own reasoning. No ads, no tracking, no account.

Gridpoint is a single-page web app. There's no server code: each visitor's browser asks the public data sources directly, so the site is just static files.

## Where every number comes from

| On screen | Source |
|---|---|
| **Now** (temperature, feels like, humidity, wind, visibility, pressure) | The newest report from the nearest official weather station (NOAA/FAA airport stations), via the Iowa Environmental Mesonet, which relays reports within minutes, or api.weather.gov, whichever is newer. The station and time are always shown. A station whose report has no wind reading (some small-airport sensors) is passed over for the next nearest that has one; if none nearby measures wind, wind shows as "--", never as calm. If the newest report is over two hours old, Gridpoint shows the forecast for this hour and says so. |
| **Hourly and days 1–7** | The National Weather Service forecast for the place's 2.5 km grid square, issued by its local NWS office (api.weather.gov). |
| **Daily high/low** | The NWS pairing: a day's high (7 AM–7 PM) with the low of the night that follows. Today's high switches to the measured high once the measured high beats the forecast. |
| **Days 8–11** | NOAA's National Blend of Models (lower 48) or NOAA's GFS model (Alaska, Hawaii, Puerto Rico), via Open-Meteo, built into days the same way NWS builds them. Labeled as model guidance. |
| **Averages** | NOAA's official 1991–2020 daily normal high and low, from the Regional Climate Centers' ACIS service (data.rcc-acis.org, the source weather.gov's NOWData uses), at the nearest station within 40 miles whose elevation is within 500 ft of the grid square's. The station and distance are shown; with no such station, the tile is left out. |
| **Tile details** | Tapping a tile opens today's chart for it: measured values (gray) from the same station, then the NWS forecast. Visibility's forecast comes from the NWS grid (about a day ahead). NWS doesn't forecast pressure, so the pressure chart shows only measured values. |
| **Feels like** | NWS wind chill (≤ 50°F, wind ≥ 3 mph) and NWS heat index (≥ 80°F). |
| **Sunrise / sunset** | NOAA solar calculator equations, solved at each event (within about 20 seconds of the astral library). |
| **Moon phases** | Meeus, *Astronomical Algorithms*, ch. 49 (within a minute or two of the ephem library). |
| **Radar** | The NWS radar loop for the place's radar site. |
| **Forecaster's discussion** | The latest Area Forecast Discussion from the local NWS office. |
| **Place search** | A built-in list of U.S. places (`places.json`: every place in GeoNames with 500+ people, plus Puerto Rico, Guam and the U.S. Virgin Islands), so results show on every keystroke and work offline; bigger and nearer places rank first. ZIP codes come from `zips/` (the zipcodes package). A place too small for the list is looked up with Open-Meteo's geocoder. `data/build_places.py` rebuilds the files. |

weather.gov answers from many servers, and for a while after each forecast update some of them still hand out the previous version. Each forecast product says which update it's built from, so if the 7-day, hourly and grid products disagree, Gridpoint asks again for the older ones and keeps the newest.

## Languages

Gridpoint's own words come in 14 languages: English, Español, 简体中文, 繁體中文, Tiếng Việt, Tagalog, 한국어, Français, Português, Deutsch, Italiano, Ελληνικά, 日本語 and Русский. **Automatic** (the default) follows the device's language list and falls back to English; the globe button at the top, or Language under About, picks one by hand. Dates, times and numbers follow the language too.

Each language's words are in `i18n/<code>.json`, keyed by the English text. NWS short forecasts ("Chance Showers And Thunderstorms then Mostly Sunny"), station reports, period names and alert names are translated from their parts: whole phrases (`c:`), weather nouns (`n:`, with a second grammatical case after `|` where a language needs one), the qualifiers around them (`q:`, with `{x}` for the noun), alert kinds and subjects (`at:`, `ap:`), and NWS period names (`pn:`). Wording NWS adds later that isn't covered falls back to the icon's words, so it still shows in the chosen language. The forecast paragraphs, alert details and the forecasters' discussion are written by the National Weather Service in English only, so they stay in English and are marked as English for screen readers.

To change a word, edit it in every `i18n/<code>.json`; to add one, wrap it in `$t('…')` in `index.html` and add it to every file. `check.py --only languages` makes sure every file has every word with its `{placeholders}`, then opens each language on a phone-sized screen.

## Report a bug

Report a bug (under About, and at the bottom of the page) opens a short form. Gridpoint has no server, so the report goes out from the visitor's own email app: the form fills in a message to swagmasterfive@emersonxia.lol with the subject "Gridpoint bug report: …", plus details that help reproduce the problem (the place and its NWS forecast area and station, browser, screen size, recent script errors). The visitor sees everything before sending and can leave the details out. To file these automatically, add a Mail rule in iCloud (Subject contains "Gridpoint bug report" → move to a "Gridpoint bug reports" folder).

## Put it online

Any static host works. Two easy ones:

**Cloudflare Pages + GitHub (updates go live on every push):** put these files in a GitHub repository, then in Cloudflare go to Workers & Pages → Create → Pages → Connect to Git, pick the repository, leave the build command empty and the output directory as `/`, and deploy. Add a custom domain (for example `gridpoint.yourdomain.com`) under the project's Custom domains tab.

**GitHub Pages:** in the repository's Settings → Pages, deploy from the `main` branch, root folder. For a custom subdomain, add a CNAME record pointing to `<your-username>.github.io` and enter the domain in the Pages settings.

## Use it like an app, offline included

Send people to **/install** (for example `gridpoint.emersonxia.lol/install`). It opens the app with a short guide for their device: iPhone and iPad (Share → Add to Home Screen), Android (one-tap Install where Chrome offers it), Mac (Safari's File → Add to Dock), and Chrome or Edge on a computer. Phones also get a small, dismissible tip on the main screen, and the guide is linked under About. No App Store and no developer account are needed.

After the first visit, the app is saved on the device (service worker) along with each place's last forecast, so it opens without a connection and says when it's showing saved data. The guide is part of the app rather than a separate page, so it works offline too, and the address stays the app's own while someone follows it (that's the address the Home Screen icon opens). On iPhone, iPad and Mac, the installed app keeps its own list of places, separate from Safari; the guide says so.

## Type

The page asks for Helvetica Now first (Display for large numbers, Text for body, Micro for chart labels), then Helvetica Neue, which ships on every Mac and iPhone. Helvetica Now is a commercial Monotype typeface; to serve it to every visitor you need a Monotype web license and the webfont files.

## Checking it

`check.py` loads the app in a headless browser for 27 places across every U.S. time zone (including Alaska, Hawaii and Puerto Rico) and compares what's on screen with the sources, re-derived independently: the station reading and every tile, the hourly strip, each daily high/low/chance, the extended outlook, sunrise/sunset (astral), moon phases (ephem), radar, alerts, metric units, chart scrubbing, the page lock behind sheets, offline use, and the Home Screen guide on iPhone, Android and a computer, place search, the bug report form, and all 14 languages. When weather.gov's servers disagree about the latest forecast, it checks the app against each version they hand out.

```
pip install playwright astral ephem
python3 -m playwright install chromium
python3 check.py                  # all places, from the files in this folder
python3 check.py --quick          # 7-place sample
python3 check.py --only install   # just the Home Screen guide
python3 check.py --only languages # just the languages
python3 check.py --url https://gridpoint.emersonxia.lol/   # the live site
```

WARN lines are worth a look but are often timing: a new station report or forecast can land between the app's request and the check's.

## Data credits

National Weather Service / NOAA (api.weather.gov, public domain) · Iowa Environmental Mesonet, Iowa State University · Climate normals from NOAA NCEI via the Regional Climate Centers' ACIS · Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0), free for non-commercial use · Place names from [GeoNames](https://www.geonames.org/) (CC BY 4.0) · ZIP codes from the [zipcodes](https://www.npmjs.com/package/zipcodes) package (BSD).
