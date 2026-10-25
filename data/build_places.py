"""Build Gridpoint's built-in place search index from GeoNames (CC BY 4.0) and the zipcodes
package (BSD). Writes places.json (U.S. places with 500+ people, plus Puerto Rico, Guam and the
U.S. Virgin Islands) and zips/0.json ... zips/9.json (ZIP codes by first digit).
Usage: python3 build_places.py <folder with cities500.txt admin1CodesASCII.txt admin2Codes.txt zipcodes codes.js> <out folder>"""
import json, os, re, sys
src, out = sys.argv[1], sys.argv[2]
KEEP = {'PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4', 'PPLC', 'PPLL', 'PPLS', 'PPLX', 'PPLG', 'PPLF', 'PPLR'}
TERR = {'PR': 'Puerto Rico', 'GU': 'Guam', 'VI': 'U.S. Virgin Islands'}

admin1 = {}
for line in open(f'{src}/admin1CodesASCII.txt', encoding='utf-8'):
    code, name, ascii_name, gid = line.rstrip('\n').split('\t')
    admin1[code] = name
admin2 = {}
for line in open(f'{src}/admin2Codes.txt', encoding='utf-8'):
    code, name, ascii_name, gid = line.rstrip('\n').split('\t')
    if code[:3] in ('US.', 'PR.', 'GU.', 'VI.'):
        admin2[code] = name

rows = []
for line in open(f'{src}/cities500.txt', encoding='utf-8'):
    f = line.rstrip('\n').split('\t')
    cc, fcode = f[8], f[7]
    if cc not in ('US', 'PR', 'GU', 'VI') or fcode not in KEEP:
        continue
    name, lat, lon, a1, a2, pop = f[1], float(f[4]), float(f[5]), f[10], f[11], int(f[14] or 0)
    if cc == 'US':
        region, region_name = a1, admin1.get(f'US.{a1}', a1)
        county = admin2.get(f'US.{a1}.{a2}', '')
    else:
        region, region_name = cc, TERR[cc]
        # in GeoNames the territories' first-level divisions are the municipios / districts
        county = admin1.get(f'{cc}.{a1}', '') if a1 and a1 != '00' else ''
        if cc == 'PR' and county and not county.endswith('Municipio'):
            county += ' Municipio'
    if not re.fullmatch(r'[A-Z]{2}', region):
        continue
    rows.append((name, region, region_name, county, lat, lon, pop))

# one entry per name + region + spot (GeoNames has a few near-duplicates)
seen, uniq = set(), []
for r in sorted(rows, key=lambda r: -r[6]):
    k = (r[0].lower(), r[1], round(r[4], 1), round(r[5], 1))
    if k in seen:
        continue
    seen.add(k)
    uniq.append(r)
uniq.sort(key=lambda r: (r[1], r[0]))

regions = sorted({(r[1], r[2]) for r in uniq})
ri = {code: i for i, (code, _) in enumerate(regions)}
counties = sorted({r[3] for r in uniq if r[3]})
ci = {c: i + 1 for i, c in enumerate(counties)}   # 0 = none
lines = [f"{r[0]}|{ri[r[1]]}|{ci.get(r[3], 0)}|{round(r[4] * 1000)}|{round(r[5] * 1000)}|{round(r[6] / 100)}" for r in uniq]
doc = {
    'v': 1,
    'about': 'Place names from GeoNames (geonames.org), CC BY 4.0: U.S. places with 500 or more people, plus Puerto Rico, Guam and the U.S. Virgin Islands. Fields: name|region|county|lat*1000|lon*1000|population/100.',
    'regions': [[c, n] for c, n in regions],
    'counties': [''] + counties,
    'rows': '\n'.join(lines),
}
os.makedirs(out, exist_ok=True)
json.dump(doc, open(f'{out}/places.json', 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))

# ZIP codes: USPS city name and a center point, split by first digit
codes = open(f'{src}/codes.js', encoding='utf-8').read()
codes, _ = json.JSONDecoder().raw_decode(codes, codes.index('{'))
valid = {c for c, _ in regions}
by = {str(d): [] for d in range(10)}
for z, v in sorted(codes.items()):
    st = v.get('state')
    if not re.fullmatch(r'\d{5}', z) or st not in valid or v.get('latitude') is None:
        continue
    # skip ZIPs that belong to a business or a mail facility rather than a town
    if re.search(r'\b(Ctr|Bank|Brm|Inc|Corp|Corporation|Company|Insurance|Service|Services|Svc|Svcs|Business|Reply|Dept|Irs|Processing|Distribution|Postal|Mail|Mailing|Fpo|Apo|Ndc|Amf|Bmc|Hq|Headquarters)\b', str(v['city']), re.I):
        continue
    city = ' '.join(w if w.isupper() and len(w) <= 3 and w not in ('NEW', 'OLD', 'BAY', 'BIG', 'FOX', 'OAK', 'ELM', 'RED', 'SUN', 'LAS', 'LOS', 'DEL', 'SAN', 'FAR', 'MAY') else w.capitalize() for w in str(v['city']).split())
    city = re.sub(r"\bMc([a-z])", lambda m: 'Mc' + m.group(1).upper(), city)
    by[z[0]].append(f"{z}|{city}|{ri[st]}|{round(float(v['latitude']) * 1000)}|{round(float(v['longitude']) * 1000)}")
os.makedirs(f'{out}/zips', exist_ok=True)
for d, ls in by.items():
    json.dump({'v': 1, 'about': 'ZIP codes from the zipcodes package (BSD license). Fields: zip|city|region|lat*1000|lon*1000.', 'rows': '\n'.join(ls)},
              open(f'{out}/zips/{d}.json', 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print(len(uniq), 'places,', len(regions), 'regions,', len(counties), 'counties;', sum(len(v) for v in by.values()), 'ZIPs')
