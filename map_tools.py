"""Map data stays in tool artifacts and graph state, outside the LLM context."""

import hashlib
import json
import re
from typing import Annotated, Literal
from uuid import uuid4

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import MessagesState
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import Field


def merge_layers(current: dict, changes: dict) -> dict:
    layers = dict(current or {})
    for key, value in changes.items():
        if value is None:
            layers.pop(key, None)
        else:
            layers[key] = value
    return layers


class GeoState(MessagesState):
    map_layers: Annotated[dict, merge_layers]
    map_camera: Annotated[dict, lambda old, new: new]


def feature(geometry, **properties):
    return {'type': 'Feature', 'geometry': geometry, 'properties': properties}


def collection(features):
    return {'type': 'FeatureCollection', 'features': features}


def map_result(content: dict, variants: dict):
    """Stable refs survive checkpoints; geometry is only in the ToolMessage artifact."""
    digest = hashlib.sha256(json.dumps(variants, sort_keys=True).encode()).hexdigest()[:24]
    ref = f'map-{digest}'
    return ({**content, 'data_ref': ref, 'map_variants': list(variants),
             'map_feature_count': {key:len(value['features']) for key,value in variants.items()}},
            {'data_ref': ref, 'collections': variants})


def decode_polyline(encoded: str):
    coordinates, lat, lon, index = [], 0, 0, 0
    while index < len(encoded):
        delta = []
        for _ in range(2):
            value = shift = 0
            while True:
                if index >= len(encoded) or shift > 30:
                    raise ValueError('Invalid route polyline')
                byte = ord(encoded[index]) - 63
                index += 1
                if not 0 <= byte <= 63:
                    raise ValueError('Invalid route polyline')
                value |= (byte & 31) << shift
                shift += 5
                if byte < 32:
                    break
            delta.append(~(value >> 1) if value & 1 else value >> 1)
        lat += delta[0]
        lon += delta[1]
        coordinates.append([lon / 1e5, lat / 1e5])
    return coordinates


@tool
def update_map(
    operation: Literal['upsert', 'remove', 'clear', 'focus', 'hide', 'show'],
    layer_id: str,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    title: str = '',
    data_ref: str | None = None,
    variant: str = 'default',
    kind: Literal['geojson', 'points', 'cones'] = 'geojson',
    color: str = '#2563eb',
    fit_bounds: bool = True,
    radius_m: Annotated[float, Field(ge=1, le=5000)] = 150,
    height_m: Annotated[float, Field(ge=1, le=3000)] = 100,
    feature_indices: list[int] | None = None,
) -> Command:
    """Control the user map using VERIFIED data from an earlier tool's data_ref.
    upsert adds/replaces a layer; choose a map_variants key from that result.
    geojson shows original geometry, points shows object locations, cones adds
    illustrative 3D cones at those locations (radius_m/height_m are user-specified
    dimensions, NOT measured elevation or terrain visibility). Use explicit user
    dimensions; disclose defaults if absent. Never invent locations or elevation.
    focus frames an existing layer; hide/show toggles it; remove deletes it;
    clear deletes all layers (layer_id may be any nonempty name).
    Stable layer_id lets follow-ups reuse layers. title is a short Russian label.
    feature_indices optionally selects 0-based features in the chosen variant;
    for search_osm these have the same order as the returned features. If the
    user requested N objects, show only those N (or use limit=N in the search).
    Reuse an existing layer's feature_indices when visualizing its same objects.
    Map state persists with the conversation; raw geometry is not in chat text.
    """
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', layer_id):
        raise ValueError('layer_id must contain 1..64 letters, digits, _ or -')
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValueError('color must be a hex RGB color')
    layers = state.get('map_layers', {})
    changes = {}
    camera = None
    if operation == 'upsert':
        if layer_id not in layers and len(layers) >= 20:
            raise ValueError('Map has 20 layers; remove unused layers first')
        artifact = next((m.artifact for m in reversed(state['messages'])
                         if isinstance(m, ToolMessage) and isinstance(m.artifact, dict)
                         and m.artifact.get('data_ref') == data_ref), None)
        if artifact is None or variant not in artifact['collections']:
            raise ValueError('Use a data_ref and variant from an earlier geographic tool result')
        data = artifact['collections'][variant]
        if feature_indices is not None:
            if not feature_indices or any(index < 0 or index >= len(data['features']) for index in feature_indices):
                raise ValueError('feature_indices must refer to existing features in the selected variant')
            data = collection([data['features'][index] for index in dict.fromkeys(feature_indices)])
        if not data['features']:
            raise ValueError('No geographic objects available to display')
        if kind in ('points', 'cones'):
            data = collection([feature(f['geometry'] if f['geometry']['type'] == 'Point'
                                       else {'type': 'Point', 'coordinates': f['properties']['center']},
                                       **f['properties'])
                               for f in data['features']
                               if f['geometry']['type'] == 'Point' or 'center' in f['properties']])
            if not data['features']:
                raise ValueError('This result has no object locations; use geojson')
        changes[layer_id] = {'id': layer_id, 'title': (title or layer_id)[:100],
                             'kind': kind, 'color': color, 'visible': True, 'data': data,
                             'radius_m': radius_m, 'height_m': height_m,
                             'illustrative': kind == 'cones', 'data_ref': data_ref, 'variant': variant,
                             'feature_indices': feature_indices}
    elif operation == 'clear':
        changes = {key: None for key in layers}
    else:
        if layer_id not in layers:
            raise ValueError('Unknown layer_id; use a layer from the current map state')
        if operation == 'remove':
            changes[layer_id] = None
        elif operation in ('hide', 'show'):
            changes[layer_id] = {**layers[layer_id], 'visible': operation == 'show'}
    if operation in ('upsert', 'focus') and fit_bounds:
        camera = {'layer_id': layer_id, 'revision': str(uuid4()),
                  'pitch': 55 if changes.get(layer_id, layers.get(layer_id, {})).get('kind') == 'cones' else 0}
    result = {'operation': operation, 'layer_id': layer_id, 'status': 'ok'}
    if operation == 'upsert':
        result['feature_count'] = len(data['features'])
        result['feature_names'] = [f['properties'].get('name') for f in data['features']]
    if operation == 'upsert' and kind == 'cones':
        result['note'] = f'Illustrative cones: radius {radius_m} m, height {height_m} m; not terrain visibility or measured height.'
    update = {'map_layers': changes, 'messages': [ToolMessage(
        content=json.dumps(result, ensure_ascii=False), tool_call_id=tool_call_id)]}
    if camera:
        update['map_camera'] = camera
    return Command(update=update)
