"""Local OpenTripPlanner client for the modelled Bustime-derived GTFS snapshot."""
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')
OTP_URL = os.getenv('OTP_URL', 'http://127.0.0.1:8090').rstrip('/')
DATA_DIR = Path(os.getenv('TRANSIT_DATA_DIR', '.cache/transit'))
CACHE_DIR = Path(os.getenv('CACHE_DIR', '.cache')) / 'transit-plans'


def departure_time(value=None):
    if value is None:
        return (datetime.now(MOSCOW) + timedelta(minutes=1)).replace(second=0, microsecond=0)
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('departure_time must include timezone, e.g. 2026-10-27T09:00:00+03:00')
    return value.astimezone(MOSCOW)


def plan_transit(start_lat, start_lon, end_lat, end_lon, at=None):
    at = departure_time(at)
    metadata = json.loads((DATA_DIR / 'kazan.gtfs.json').read_text())
    if not metadata['calendar_start'] <= at.date().isoformat() <= metadata['calendar_end']:
        raise ValueError(f"Departure date outside model calendar {metadata['calendar_start']}..{metadata['calendar_end']}")
    params = [OTP_URL,metadata['gtfs_sha256'],start_lat,start_lon,end_lat,end_lon,at.isoformat(),2]
    CACHE_DIR.mkdir(parents=True,exist_ok=True)
    path = CACHE_DIR / (hashlib.sha256(json.dumps(params).encode()).hexdigest() + '.json')
    if path.exists():
        return {**json.loads(path.read_text()),'cache_hit':True}
    query = '''{
      planConnection(
        origin: {location: {coordinate: {latitude: START_LAT, longitude: START_LON}}}
        destination: {location: {coordinate: {latitude: END_LAT, longitude: END_LON}}}
        dateTime: {earliestDeparture: DEPARTURE}
        modes: {direct: [WALK], transit: {transit: [{mode: BUS}, {mode: TRAM}, {mode: SUBWAY}]}}
        first: 3
      ) { edges { node { start end legs {
        mode
        from { name lat lon departure { scheduledTime } }
        to { name lat lon arrival { scheduledTime } }
        route { gtfsId shortName longName }
        legGeometry { points }
      } } } }
    }'''
    for key, value in {'START_LAT':float(start_lat),'START_LON':float(start_lon),'END_LAT':float(end_lat),'END_LON':float(end_lon),'DEPARTURE':at.isoformat()}.items():
        query = query.replace(key,json.dumps(value))
    # urllib avoids geo_tools' indefinite global requests-cache for time-dependent plans.
    request = Request(OTP_URL + '/otp/gtfs/v1',data=json.dumps({'query':query}).encode(),headers={'Content-Type':'application/json'})
    with urlopen(request,timeout=90) as response:
        payload = json.load(response)
    if payload.get('errors'):
        raise RuntimeError('OpenTripPlanner: '+json.dumps(payload['errors'],ensure_ascii=False))
    itineraries = []
    for edge in payload['data']['planConnection']['edges']:
        node = edge['node']
        legs = []
        for leg in node['legs']:
            route = leg.get('route')
            dep = leg['from'].get('departure',{}).get('scheduledTime')
            arr = leg['to'].get('arrival',{}).get('scheduledTime')
            legs.append({
                'mode':leg['mode'],'route':route['shortName'] if route else None,
                'route_id':route['gtfsId'] if route else None,
                'route_url':route_link(route['gtfsId']) if route else None,
                'from':{k:leg['from'][k] for k in ('name','lat','lon')},
                'to':{k:leg['to'][k] for k in ('name','lat','lon')},
                'departure_time':dep,'arrival_time':arr,
                'geometry':leg.get('legGeometry'),
            })
        transit_count = sum(leg['mode'] != 'WALK' for leg in legs)
        itineraries.append({
            'departure_time':node['start'],'arrival_time':node['end'],
            'duration_minutes':round((datetime.fromisoformat(node['end'])-datetime.fromisoformat(node['start'])).total_seconds()/60,1),
            'initial_wait_minutes':round(max(0,(datetime.fromisoformat(node['start'])-at).total_seconds()/60),1),
            'total_minutes_from_requested_departure':round((datetime.fromisoformat(node['end'])-at).total_seconds()/60,1),
            'contains_transit':bool(transit_count),'transfers':max(0,transit_count-1),'legs':legs,
        })
    result = {
        'itineraries':itineraries,'requested_departure_time':at.isoformat(),'estimated':True,
        'source':'Bustime-derived GTFS + OpenStreetMap; local OpenTripPlanner',
        'source_url':metadata['source_url'],'source_retrieved_at':metadata['retrieved_at'],
        'retrieved_at':datetime.now(MOSCOW).isoformat(),'feed_sha256':metadata['gtfs_sha256'],
        'calendar_start':metadata['calendar_start'],'calendar_end':metadata['calendar_end'],
        'coverage':{'routes':metadata['routes'],'stops':metadata['stops']},
        'limitations':[
            'Prototype, not an official timetable. Only source departures are retained; intermediate and final times are modelled.',
            'Weekly service masks projected onto a local calendar; public holidays and cancellations are unknown. No live vehicle data.',
            'OSM walking access and transfers; no wheelchair-accessibility guarantee. Feed coverage is incomplete.',
        ],
        'cache_hit':False,
    }
    # No-route results are not persisted, so a transient routing problem is retryable.
    if itineraries:
        import tempfile
        with tempfile.NamedTemporaryFile('w',dir=CACHE_DIR,delete=False) as stream:
            json.dump(result,stream,ensure_ascii=False);temporary = Path(stream.name)
        temporary.replace(path)
    return result


def route_link(gtfs_id):
    # Stable link from route ID back to the actual source route; no inferred names.
    try:
        snapshot = json.loads((DATA_DIR / 'bustime-source.json').read_text())
        row = snapshot['bus'][gtfs_id.rsplit(':',1)[-1]]
        slug = row[snapshot['bus__meta']['fields'].index('slug')]
        return f'https://ru.busti.me/kazan/{slug}/'
    except (FileNotFoundError,KeyError,ValueError):
        return 'https://ru.busti.me/kazan/timetable/'
