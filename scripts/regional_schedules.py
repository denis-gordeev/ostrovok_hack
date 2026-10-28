"""Cache and parse published Innopolis timetables, then merge a small GTFS feed.

Geocoded stops and route geometry must be provided from verified cached OSM data.
No location, timetable or route geometry is inferred by an LLM.
"""
import argparse
import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.bustime_to_gtfs import clock, seconds

INNOPOLIS_URL = 'https://innopolis.com/ru/city/bus-schedule'
SVIYAZHSK_URL = 'https://ostrovgrad.ru/kak-dobratsya-v-sviyazhsk/'


def snapshot(url, folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    path=folder/(hashlib.sha256(url.encode()).hexdigest()+'.html')
    if not path.exists():
        response=requests.get(url,timeout=60,headers={'User-Agent':'ostrovok-kazan-transit/0.1'})
        response.raise_for_status()
        path.write_bytes(response.content)
    return path, {'url':url,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                  'retrieved_at':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()}


def normalize_name(name):
    return name.removeprefix('Казань, ').replace('Иннополис, Школа','Школа (Иннополис)')


def parse_innopolis(html, mask, source_url):
    records=[]
    for card in BeautifulSoup(html,'html.parser').select('.schedule_item'):
        stops=card.select('.schedule_item__station')
        if len(stops)!=2:
            raise ValueError('Expected exactly two timetable endpoints')
        ref=card.select_one('.schedule_item__autobus span').get_text(strip=True)
        if ref not in ('106','108'):
            raise ValueError(f'Unexpected route {ref}')
        dep=seconds(stops[0].select_one('.schedule_item__time').get_text(strip=True))
        arr=seconds(stops[1].select_one('.schedule_item__time').get_text(strip=True))
        if arr<dep:arr+=86400
        if not 0<arr-dep<=4*3600:
            raise ValueError('Unexpected Innopolis trip duration')
        records.append({'route_id':f'official-inno-{ref}','ref':ref,'route_type':3,
                        'from':normalize_name(stops[0].select_one('span').get_text(strip=True)),
                        'to':normalize_name(stops[1].select_one('span').get_text(strip=True)),
                        'departure':clock(dep),'arrival':clock(arr),'mask':mask,
                        'arrival_estimated':False,'source_url':source_url})
    if not records:
        raise ValueError('No timetable rows found; source layout may have changed')
    return records


def collect(folder):
    records=[];sources=[]
    for direction in ('from','to'):
        for weekend,mask in [('no','1111100'),('yes','0000011')]:
            url=f'{INNOPOLIS_URL}?direction={direction}&weekend={weekend}'
            path,source=snapshot(url,folder)
            records.extend(parse_innopolis(path.read_text(),mask,url));sources.append(source)
    # Separate weekday/weekend service is preserved; exact duplicates are removed.
    unique={json.dumps(r,sort_keys=True):r for r in records}
    path,source=snapshot(SVIYAZHSK_URL,folder)
    text=' '.join(BeautifulSoup(path.read_text(),'html.parser').stripped_strings)
    def departures(marker, end_marker):
        if marker not in text:raise ValueError(f'Timetable marker missing: {marker}')
        part=text.split(marker,1)[1].split(end_marker,1)[0]
        times=[f'{int(h):02d}:{m}:00' for h,m in re.findall(r'(\d{1,2})[.:](\d{2})',part)]
        if not times:raise ValueError('No published departures found')
        return times
    pairs=[('Свияжск — станция','Свияжск — остров',
            departures('Н. Вязовые (ж/д станция Свияжск) (до О. Свияжск):','Сайт:')),
           ('Свияжск — остров','Свияжск — станция',
            departures('О. Свияжск (до Н. Вязовых):','Пункт отправления:'))]
    extra=[]
    for origin,destination,times in pairs:
        for dep in times:
            extra.append({'route_id':'official-sviyazhsk-407','ref':'407','route_type':3,
                          'from':origin,'to':destination,'departure':dep,'arrival':None,
                          'arrival_estimated':True,'mask':'1111111','source_url':SVIYAZHSK_URL})
    # The current museum page lists times but no weekday calendar for 407.
    # Keep the daily projection explicitly unconfirmed, never call it official validity.
    source['calendar_note']='407: daily service is a prototype assumption; weekday operation is not confirmed by this page.'
    sources.append(source)
    return {'records':[*unique.values(),*extra],'sources':sources,
            'calendar_assumptions':['407: days of operation are unconfirmed; daily service is assumed for this prototype. Verify with the operator.']}


def merge(base, output, supplement, locations, geometries):
    """Merge into one feed so OTP and API use a single versioned provenance report."""
    base,output=Path(base),Path(output)
    tables={}
    with zipfile.ZipFile(base) as z:
        for name in z.namelist():
            rows=list(csv.reader(io.StringIO(z.read(name).decode('utf-8-sig'))))
            tables[name]=(rows[0],rows[1:])
    metadata=json.loads(base.with_suffix('.json').read_text())
    calendar={row[0] for row in tables['calendar.txt'][1]}
    seen=set();used_stops=set();used_routes=set();shape_ids={};count=0
    def add(table,row):tables[table+'.txt'][1].append(row)
    for record in supplement['records']:
        key=(record['from'],record['to'])
        if any(name not in locations for name in key):
            raise ValueError(f'Missing verified stop coordinates: {key}')
        geometry=geometries.get('|'.join(key))
        if not geometry or len(geometry['coordinates'])<2:
            raise ValueError(f'Missing verified route geometry: {key}')
        if record['arrival'] is None:
            # Explicitly modelled at 35 km/h, no claim of a published arrival time.
            record={**record,'arrival':clock(seconds(record['departure'])+max(60,round(geometry['length_m']/35*3.6))),
                    'arrival_estimated':True}
        ident=(record['route_id'],record['mask'],*key,record['departure'],record['arrival'])
        if ident in seen:continue
        seen.add(ident)
        mask=record['mask']
        if mask not in calendar:
            add('calendar',[mask,*mask,metadata['calendar_start'].replace('-',''),metadata['calendar_end'].replace('-','')]);calendar.add(mask)
        shape_key='|'.join(key)
        if shape_key not in shape_ids:
            sid='official-shape-'+str(len(shape_ids)+1);shape_ids[shape_key]=sid
            for seq,(lon,lat) in enumerate(geometry['coordinates'],1):
                # shape_dist_traveled intentionally omitted for these supplemental shapes.
                add('shapes',[sid,lat,lon,seq,''])
        for name in key:
            if name not in used_stops:
                location=locations[name]
                add('stops',[location['id'],name,location['lat'],location['lon']]);used_stops.add(name)
        rid=record['route_id']
        if rid not in used_routes:
            add('routes',[rid,'bustime-kazan',record['ref'],'',record['route_type'],record['source_url'],
                          'Published endpoint departures; arrival may be modelled; no intermediate boarding. Cached prototype, verify operating calendar.'])
            used_routes.add(rid)
        count+=1;tid=f'official-trip-{count}'
        add('trips',[rid,mask,tid,key[1],0 if 'Иннополис' in key[1] or 'Свияжск — остров' == key[1] else 1,shape_ids[shape_key]])
        add('stop_times',[tid,record['departure'],record['departure'],locations[key[0]]['id'],1,'',1])
        add('stop_times',[tid,record['arrival'],record['arrival'],locations[key[1]]['id'],2,'',0 if record['arrival_estimated'] else 1])
    output.parent.mkdir(parents=True,exist_ok=True)
    if 'feed_info.txt' in tables:
        version_index=tables['feed_info.txt'][0].index('feed_version')
        for row in tables['feed_info.txt'][1]:
            row[version_index] += '-official-'+hashlib.sha256(json.dumps(supplement,sort_keys=True).encode()).hexdigest()[:10]
    temporary=output.with_suffix('.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as z:
        for name,(fields,rows) in tables.items():
            stream=io.StringIO();writer=csv.writer(stream);writer.writerow(fields);writer.writerows(rows)
            z.writestr(name,stream.getvalue())
    metadata.update({'routes':metadata['routes']+len(used_routes),'stops':metadata['stops']+len(used_stops),
                     'trips':metadata['trips']+count,'stop_times':metadata['stop_times']+2*count,
                     'additional_sources':supplement['sources'],
                     'route_sources':{r['route_id']:r['source_url'] for r in supplement['records']},
                     'supplemental_geometry_sources':geometries,
                     'supplemental_stop_sources':locations,
                     'supplemental_note':'Only published endpoint boarding/alighting, no inferred intermediate stops. Road-derived geometry is modelled, not a verified bus trace.',
                     'calendar_assumptions':supplement.get('calendar_assumptions',[]),
                     'route_limitations':{rid:[
                         'Only published endpoints are served in this prototype. Intermediate boarding is not included; displayed bus geometry is modelled from roads and can differ from the real route.',
                         *(supplement.get('calendar_assumptions',[]) if rid=='official-sviyazhsk-407' else []),
                     ] for rid in used_routes},
                     'gtfs_sha256':hashlib.sha256(temporary.read_bytes()).hexdigest()})
    temporary.replace(output)
    output.with_suffix('.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
    return metadata


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cache',type=Path,default=Path('.cache/transit/official-snapshots'))
    p.add_argument('--collect',type=Path,help='Write normalized Innopolis records and source checksums')
    p.add_argument('--base',type=Path);p.add_argument('--output',type=Path)
    p.add_argument('--supplement',type=Path);p.add_argument('--locations',type=Path);p.add_argument('--geometries',type=Path)
    a=p.parse_args()
    if a.collect:
        a.collect.parent.mkdir(parents=True,exist_ok=True)
        result=collect(a.cache);a.collect.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('Collected departures:',len(result['records']))
    else:
        if not all([a.base,a.output,a.supplement,a.locations,a.geometries]):p.error('Provide all merge inputs')
        result=merge(a.base,a.output,*[json.loads(p.read_text()) for p in (a.supplement,a.locations,a.geometries)])
        print(json.dumps({k:result[k] for k in ['routes','stops','trips','gtfs_sha256']},ensure_ascii=False))


if __name__=='__main__':main()
