"""Convert a cached Bustime city JSON into an explicitly modelled GTFS feed."""
import argparse
import ast
import csv
import hashlib
import io
import json
import math
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from shapely.geometry import LineString, Point

SOURCE_URL = 'https://sel.bustm.net/static/other/db/v8/current/10.json'
BOUNDS = (48.75, 55.55, 49.55, 56.10)
ROUTE_TYPES = {0: 3, 1: 11, 2: 0, 7: 1}
SPEEDS_KMH = {0: 18, 1: 16, 2: 16, 7: 32}
DWELL_SECONDS = 20
VERSION = 1


def rows(data, table):
    fields = data[table + '__meta']['fields']
    values = data[table]
    if isinstance(values, dict):
        return [dict(zip(fields, value), id=str(key)) for key, value in values.items()]
    return [dict(zip(fields, value)) for value in values]


def inside(stop):
    return BOUNDS[0] <= stop['point_x'] <= BOUNDS[2] and BOUNDS[1] <= stop['point_y'] <= BOUNDS[3]


def seconds(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,2}:\d{2}(:\d{2})?', value):
        raise ValueError(f'Invalid departure: {value!r}')
    parts = [int(v) for v in value.split(':')]
    h, m, s = *parts[:2], parts[2] if len(parts) == 3 else 0
    if h > 47 or m > 59 or s > 59:
        raise ValueError(f'Invalid departure: {value!r}')
    return h * 3600 + m * 60 + s


def clock(value):
    return f'{value // 3600:02d}:{value % 3600 // 60:02d}:{value % 60:02d}'


def distances(stops, geometry):
    """Project on an ordered shape; fall back to stop chords on loops/mismatches."""
    scale_x = 111_320 * math.cos(math.radians(55.8))
    def xy(lon, lat):
        return lon * scale_x, lat * 111_320
    coords = [(s['point_x'], s['point_y']) for s in stops]
    try:
        points = ast.literal_eval(geometry) if isinstance(geometry, str) else geometry
        if not points or len(points) < 2:
            raise ValueError('No shape')
        points = [(float(lon), float(lat)) for lon, lat in points]
        if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in points):
            raise ValueError('Invalid shape coordinates')
        line = LineString([xy(*point) for point in points])
        stop_points = [Point(xy(*point)) for point in coords]
        positions = [line.project(point) for point in stop_points]
        if any(line.distance(point) > 250 for point in stop_points) or any(b <= a for a, b in zip(positions, positions[1:])):
            raise ValueError('Shape does not follow stop order')
        measures = [0.0]
        for a, b in zip(points, points[1:]):
            measures.append(measures[-1] + math.dist(xy(*a), xy(*b)))
        return points, measures, positions, False
    except (ValueError, SyntaxError, TypeError):
        measures = [0.0]
        for a, b in zip(coords, coords[1:]):
            measures.append(measures[-1] + math.dist(xy(*a), xy(*b)))
        return coords, measures, measures, True


def convert(source, output, start_date, days=30):
    if not 1 <= days <= 90:
        raise ValueError('Synthetic calendar horizon must be 1..90 days')
    raw = Path(source).read_bytes()
    data = json.loads(raw)
    stops = {s['id']: s for s in rows(data, 'nbusstop')}
    buses = {b['id']: b for b in rows(data, 'bus') if b['active'] and not b.get('hidden') and b['ttype'] in ROUTE_TYPES}
    route_stops = defaultdict(list)
    for row in rows(data, 'route'):
        route_stops[(str(row['bus_id']), row['direction'])].append(row)
    lines = {(str(r['bus_id']), r['direction']): r['line'] for r in rows(data, 'routeline')}
    patterns = {}
    skipped = Counter()
    for key, pattern in route_stops.items():
        if key[0] not in buses or key[1] not in (0, 1):
            continue
        ids = []
        for row in sorted(pattern, key=lambda r: r['order']):
            sid = str(row['busstop_id'])
            if not ids or ids[-1] != sid:
                ids.append(sid)
        if len(ids) < 2 or any(sid not in stops or not inside(stops[sid]) for sid in ids):
            skipped['pattern_outside_bbox_or_missing_stops'] += 1
            continue
        points, shape_distances, stop_distances, fallback = distances([stops[s] for s in ids], lines.get(key))
        offsets = [0]
        for a, b in zip(stop_distances, stop_distances[1:]):
            factor = 1.25 if fallback else 1.0
            offsets.append(offsets[-1] + max(30, round((b - a) * factor / (SPEEDS_KMH[buses[key[0]]['ttype']] / 3.6)) + DWELL_SECONDS))
        patterns[key] = (ids, points, shape_distances, stop_distances, offsets, fallback)
    departures = []
    seen = set()
    for row in rows(data, 'trip'):
        key = (str(row['bus_id']), row['direction'])
        if key not in patterns:
            skipped['departure_without_supported_pattern'] += 1
            continue
        mask = row['mask']
        if row['type'] != 1 or not isinstance(mask, str) or not re.fullmatch('[01]{7}', mask) or '1' not in mask:
            skipped['unsupported_departure_type_or_mask'] += 1
            continue
        ids = patterns[key][0]
        sid = row.get('busstop_id')
        anchor = str(sid) if sid is not None else ids[0]
        if anchor not in ids or ids.count(anchor) != 1:
            skipped['ambiguous_departure_stop'] += 1
            continue
        index = ids.index(anchor)
        if index == len(ids) - 1:
            skipped['departure_at_last_stop'] += 1
            continue
        try:
            dep = seconds(row['time'])
        except ValueError:
            skipped['invalid_departure_time'] += 1
            continue
        identity = (*key, mask, index, dep)
        if identity in seen:
            continue
        seen.add(identity)
        departures.append((key, mask, index, dep))
    if not departures:
        raise ValueError('No usable departures in snapshot')
    used_patterns = {d[0] for d in departures}
    used_buses = sorted({key[0] for key in used_patterns})
    used_stops = sorted({sid for key in used_patterns for sid in patterns[key][0]})
    masks = sorted({d[1] for d in departures})
    end_date = start_date + timedelta(days=days - 1)
    stamp = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(raw).hexdigest()
    report = {
        'schema_version': VERSION, 'source_url': SOURCE_URL, 'source_sha256': digest,
        'retrieved_at': datetime.fromtimestamp(Path(source).stat().st_mtime, timezone.utc).isoformat(),
        'generated_at': stamp, 'estimated': True, 'timezone': 'Europe/Moscow',
        'calendar_start': start_date.isoformat(), 'calendar_end': end_date.isoformat(),
        'calendar_note': f'Local {days}-day projection of weekly masks, not an operator-confirmed validity period; public holidays are unknown.',
        'timing_note': 'Only source departure times are retained. Intermediate and final times are modelled from shape distances and assumed speeds, with 20 seconds per stop. No live updates.',
        'speeds_kmh_by_bustime_type': SPEEDS_KMH, 'dwell_seconds': DWELL_SECONDS,
        'chord_distance_multiplier': 1.25, 'bbox': BOUNDS,
        'routes': len(used_buses), 'stops': len(used_stops), 'trips': len(departures),
        'patterns': len(used_patterns), 'chord_shape_patterns': sum(patterns[k][5] for k in used_patterns),
        'excluded': dict(skipped), 'routes_without_exported_trips': [buses[b]['slug'] for b in buses if b not in used_buses],
        'source_license': 'Not established; source code MIT license does not establish a data license.',
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    count_stop_times = 0
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as z:
        def write(name, fields, values):
            with z.open(name + '.txt', 'w') as raw_file:
                with io.TextIOWrapper(raw_file, encoding='utf-8', newline='') as stream:
                    w = csv.writer(stream);w.writerow(fields);w.writerows(values)
        write('agency', ['agency_id','agency_name','agency_url','agency_timezone','agency_lang'], [
            ['bustime-kazan','Kazan prototype — Bustime-derived ESTIMATED','https://ru.busti.me/kazan/','Europe/Moscow','ru']])
        write('stops', ['stop_id','stop_name','stop_lat','stop_lon'], [
            [sid,stops[sid]['name'],stops[sid]['point_y'],stops[sid]['point_x']] for sid in used_stops])
        write('routes', ['route_id','agency_id','route_short_name','route_long_name','route_type','route_url','route_desc'], [
            [bid,'bustime-kazan',buses[bid]['name'],'',ROUTE_TYPES[buses[bid]['ttype']],f"https://ru.busti.me/kazan/{buses[bid]['slug']}/",'Prototype: intermediate travel times are estimated; not an official timetable.'] for bid in used_buses])
        write('calendar', ['service_id','monday','tuesday','wednesday','thursday','friday','saturday','sunday','start_date','end_date'], [
            [mask,*mask,start_date.strftime('%Y%m%d'),end_date.strftime('%Y%m%d')] for mask in masks])
        trip_rows, time_rows = [], []
        for number, (key, mask, anchor, departure) in enumerate(sorted(departures), 1):
            ids, points, sd, stop_d, offsets, fallback = patterns[key]
            trip_id = f't{number}'
            shape_id = f'{key[0]}_{key[1]}'
            trip_rows.append([key[0],mask,trip_id,stops[ids[-1]]['name'],key[1],shape_id])
            for seq, index in enumerate(range(anchor,len(ids)),1):
                tm = clock(departure + offsets[index] - offsets[anchor])
                time_rows.append([trip_id,tm,tm,ids[index],seq,round(stop_d[index],2),1 if index == anchor else 0])
        count_stop_times = len(time_rows)
        write('trips', ['route_id','service_id','trip_id','trip_headsign','direction_id','shape_id'], trip_rows)
        write('stop_times', ['trip_id','arrival_time','departure_time','stop_id','stop_sequence','shape_dist_traveled','timepoint'], time_rows)
        write('shapes', ['shape_id','shape_pt_lat','shape_pt_lon','shape_pt_sequence','shape_dist_traveled'], (
            [f'{key[0]}_{key[1]}',lat,lon,seq,round(distance,2)]
            for key in sorted(used_patterns) for seq,((lon,lat),distance) in enumerate(zip(patterns[key][1],patterns[key][2]),1)))
        write('feed_info', ['feed_publisher_name','feed_publisher_url','feed_lang','feed_start_date','feed_end_date','feed_version'], [[
            'Local ESTIMATED prototype, based on Bustime','https://ru.busti.me/kazan/','ru',start_date.strftime('%Y%m%d'),end_date.strftime('%Y%m%d'),f'{digest[:12]}-model{VERSION}-{start_date}']])
    report['stop_times'] = count_stop_times
    report['gtfs_sha256'] = hashlib.sha256(temporary.read_bytes()).hexdigest()
    temporary.replace(output)
    output.with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True,help='Cached Bustime JSON snapshot (no implicit network download)')
    p.add_argument('--output',type=Path,default=Path('.cache/transit/kazan.gtfs.zip'))
    p.add_argument('--start-date',type=date.fromisoformat,default=datetime.now(ZoneInfo('Europe/Moscow')).date())
    p.add_argument('--days',type=int,default=30)
    a=p.parse_args()
    print(json.dumps(convert(a.source,a.output,a.start_date,a.days),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
