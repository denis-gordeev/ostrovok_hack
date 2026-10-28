"""Model supplemental bus shapes on a verified cached regional driving graph."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shapely.geometry import Point
from geo_api import DRIVE_SPEEDS,route_result
from geo_tools import ox


def build(graph_path,locations,schedules):
    graph=ox.load_graphml(graph_path)
    graph=ox.routing.add_edge_speeds(graph,hwy_speeds=DRIVE_SPEEDS,fallback=30)
    graph=ox.routing.add_edge_travel_times(graph)
    projected=ox.project_graph(graph)
    nodes=ox.graph_to_gdfs(projected,edges=False)
    snapped={}
    for name,location in locations.items():
        point=ox.projection.project_geometry(Point(location['lon'],location['lat']),to_crs=nodes.crs)[0]
        distances=nodes.geometry.distance(point)
        node=distances.idxmin();offset=float(distances.loc[node])
        if offset>300:raise ValueError(f'No driving graph node within 300 m of {name}: {offset:.0f} m')
        snapped[name]=(node,offset)
    shapes={}
    digest=hashlib.sha256(Path(graph_path).read_bytes()).hexdigest()
    for origin,destination in sorted({(r['from'],r['to']) for r in schedules['records']}):
        a,ao=snapped[origin];b,bo=snapped[destination]
        result=route_result(graph,a,b,'drive',ao,bo)
        if not result['geometry']:raise ValueError('Empty supplemental route geometry')
        shapes[origin+'|'+destination]={
            'coordinates':result['geometry']['coordinates'],'length_m':result['length_m'],
            'modelled':True,'note':'Road-derived illustrative geometry, not an operator-confirmed bus trace; no intermediate boarding inferred.',
            'source':'OpenStreetMap regional driving graph','graph_sha256':digest,
            'start_snap_distance_m':ao,'end_snap_distance_m':bo,
        }
    return shapes


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--graph',type=Path,required=True);p.add_argument('--locations',type=Path,required=True)
    p.add_argument('--schedules',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    shapes=build(a.graph,json.loads(a.locations.read_text()),json.loads(a.schedules.read_text()))
    a.output.write_text(json.dumps(shapes,ensure_ascii=False))
    print('Supplemental modelled bus shapes:',len(shapes))


if __name__=='__main__':main()
