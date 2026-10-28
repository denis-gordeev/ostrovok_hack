import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts.bustime_to_gtfs import clock, convert, distances, seconds
from transit_routing import departure_time, plan_transit


def fixture():
    tables = {
        'bus':(['active','hidden','name','slug','ttype'],{'1':[True,False,'1','bus-1',0]}),
        'nbusstop':(['name','point_x','point_y'],{
            'a':['A',49.10,55.79],'b':['B',49.11,55.79],'c':['C',49.12,55.79]}),
        'route':(['bus_id','busstop_id','direction','order'],{
            '1':[1,'a',0,0],'2':[1,'b',0,1],'3':[1,'c',0,2]}),
        'routeline':(['bus_id','direction','line'],{}),
        'trip':(['bus_id','direction','mask','time','type','busstop_id','note'],[
            [1,0,'1111100','25:10',1,None,''],
            [1,0,'1111100','25:10',1,None,''],
            [1,0,'0000011','09:00',1,'b',''],
            [1,0,'invalid','10:00',1,None,'']]),
    }
    return {key:value for name,(fields,values) in tables.items()
            for key,value in [(name,values),(name+'__meta',{'fields':fields})]}


class GtfsTests(unittest.TestCase):
    def test_calendar_midnight_partial_trip_and_estimates(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.json';source.write_text(json.dumps(fixture()))
            output=Path(folder)/'kazan.gtfs.zip'
            report=convert(source,output,date(2026,10,26))
            self.assertEqual(report['trips'],2)  # duplicate removed, malformed mask excluded
            with zipfile.ZipFile(output) as z:
                def table(name):
                    return list(csv.DictReader(io.StringIO(z.read(name+'.txt').decode())))
                calendars={r['service_id']:r for r in table('calendar')}
                self.assertEqual(calendars['1111100']['monday'],'1')
                self.assertEqual(calendars['0000011']['monday'],'0')
                self.assertEqual(calendars['0000011']['sunday'],'1')
                self.assertEqual(calendars['1111100']['end_date'],'20261124')
                times=table('stop_times')
                groups={t['trip_id']:[r for r in times if r['trip_id']==t['trip_id']] for t in table('trips')}
                self.assertEqual(sorted(len(g) for g in groups.values()),[2,3])
                for group in groups.values():
                    self.assertEqual(group[0]['timepoint'],'1')
                    self.assertTrue(all(r['timepoint']=='0' for r in group[1:]))
                    self.assertTrue(all(seconds(b['arrival_time'])>seconds(a['arrival_time']) for a,b in zip(group,group[1:])))
                self.assertIn('25:10:00',[g[0]['departure_time'] for g in groups.values()])
                self.assertTrue(report['estimated'])

    def test_loop_geometry_falls_back_instead_of_backwards_time(self):
        stops=[{'point_x':49.1,'point_y':55.79},{'point_x':49.11,'point_y':55.79},{'point_x':49.1,'point_y':55.79}]
        _,_,positions,fallback=distances(stops,'[(49.1,55.79),(49.11,55.79),(49.1,55.79)]')
        self.assertTrue(fallback)
        self.assertGreater(positions[-1],positions[0])

    def test_time_validation_and_no_service(self):
        self.assertEqual(clock(seconds('25:10')), '25:10:00')
        with self.assertRaises(ValueError): seconds('10:99')
        with self.assertRaises(ValueError): departure_time('2026-10-27T09:00')
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'empty.json';d=fixture();d['trip']=[];source.write_text(json.dumps(d))
            with self.assertRaisesRegex(ValueError,'No usable departures'):
                convert(source,Path(folder)/'out.zip',date(2026,10,26))


class PlanCacheTests(unittest.TestCase):
    def test_fixed_departure_cache_is_invalidated_by_feed_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            meta={'calendar_start':'2026-10-26','calendar_end':'2026-11-24','gtfs_sha256':'v1',
                  'source_url':'https://example.org/source','retrieved_at':'2026-10-26','routes':1,'stops':2}
            (root/'kazan.gtfs.json').write_text(json.dumps(meta))
            payload={'data':{'planConnection':{'edges':[{'node':{
                'start':'2026-10-27T09:00:00+03:00','end':'2026-10-27T09:15:00+03:00','legs':[]}}]}}}
            def respond(*args,**kwargs): return io.BytesIO(json.dumps(payload).encode())
            with patch('transit_routing.DATA_DIR',root),patch('transit_routing.CACHE_DIR',root/'cache'),patch('transit_routing.urlopen',side_effect=respond) as fetch:
                args=(55.79,49.1,55.79,49.12,'2026-10-27T09:00:00+03:00')
                self.assertFalse(plan_transit(*args)['cache_hit'])
                self.assertTrue(plan_transit(*args)['cache_hit'])
                self.assertEqual(fetch.call_count,1)
                meta['gtfs_sha256']='v2';(root/'kazan.gtfs.json').write_text(json.dumps(meta))
                self.assertFalse(plan_transit(*args)['cache_hit'])
                self.assertEqual(fetch.call_count,2)
                with self.assertRaisesRegex(ValueError,'outside model calendar'):
                    plan_transit(*args[:-1],'2027-01-01T09:00:00+03:00')


if __name__=='__main__': unittest.main()
