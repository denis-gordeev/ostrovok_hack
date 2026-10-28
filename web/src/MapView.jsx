import React, {useEffect, useRef, useState} from 'react';
import maplibregl from 'maplibre-gl';
import {MapboxOverlay} from '@deck.gl/mapbox';
import {GeoJsonLayer, TextLayer} from '@deck.gl/layers';
import {SimpleMeshLayer} from '@deck.gl/mesh-layers';
import 'maplibre-gl/dist/maplibre-gl.css';

const rgb = color => color.match(/[a-f0-9]{2}/gi).map(v => parseInt(v, 16));
const transportColors = {WALK: [100,116,139], CAR: [217,119,6], BUS: [37,99,235], TRAM: [219,92,39], SUBWAY: [126,62,190], RAIL:[190,24,93]};

// Unit cone, base at z=0 and tip at z=1. Scaled in metres by the layer.
function coneMesh() {
  const positions = [], normals = [];
  for (let i = 0; i < 48; i++) {
    const a = i * Math.PI / 24, b = (i + 1) * Math.PI / 24;
    const middle = (a + b) / 2;
    positions.push(Math.cos(a),Math.sin(a),0, Math.cos(b),Math.sin(b),0, 0,0,1);
    for (let j = 0; j < 3; j++) normals.push(Math.cos(middle)/Math.SQRT2,Math.sin(middle)/Math.SQRT2,1/Math.SQRT2);
    positions.push(0,0,0, Math.cos(b),Math.sin(b),0, Math.cos(a),Math.sin(a),0);
    for (let j = 0; j < 3; j++) normals.push(0,0,-1);
  }
  return {attributes: {positions: {size:3, value:new Float32Array(positions)}, normals: {size:3, value:new Float32Array(normals)}}};
}
const mesh = coneMesh();

function boundsFor(layer) {
  const bounds = new maplibregl.LngLatBounds();
  function visit(coordinates) {
    if (typeof coordinates[0] === 'number') bounds.extend(coordinates.slice(0,2));
    else coordinates.forEach(visit);
  }
  for (const f of layer.data.features) if (f.geometry?.coordinates) visit(f.geometry.coordinates);
  if (layer.kind === 'cones' && !bounds.isEmpty()) {
    const latMargin = layer.radius_m / 111320;
    const lonMargin = latMargin / Math.cos(bounds.getCenter().lat * Math.PI / 180);
    const sw = bounds.getSouthWest(), ne = bounds.getNorthEast();
    bounds.extend([sw.lng-lonMargin, sw.lat-latMargin]);
    bounds.extend([ne.lng+lonMargin, ne.lat+latMargin]);
  }
  return bounds;
}

export function MapView({layers, camera, onVisibility, onRemove, onSelect, selectedPoint, busy}) {
  const container = useRef(), map = useRef(), overlay = useRef(), selection = useRef();
  const [ready, setReady] = useState(false), [terrain, setTerrain] = useState(false);
  const [error, setError] = useState('');
  const [viewRevision, setViewRevision] = useState(0);
  const selectHandler = useRef(onSelect);
  selectHandler.current = onSelect;
  useEffect(() => {
    const instance = new maplibregl.Map({container: container.current,
      center:[49.1064,55.7961], zoom:12, maxZoom:19,
      style:{version:8, sources:{basemap:{type:'raster',tiles:[`${location.origin}/ui/tiles/osm/{z}/{x}/{y}.png`],tileSize:256,
        attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'}},
        layers:[{id:'basemap',type:'raster',source:'basemap'}]},
    });
    map.current = instance;
    instance.addControl(new maplibregl.NavigationControl({visualizePitch:true}), 'top-right');
    const deck = new MapboxOverlay({interleaved:false, layers:[],
      getTooltip: ({object}) => object ? {text:object.properties?.name || 'Объект на карте'} : null,
      onClick: ({object}) => {
        if (!object) return;
        const content = document.createElement('div');
        const name = document.createElement('strong');
        name.textContent = object.properties?.name || 'Объект на карте';
        content.append(name);
        const url = object.properties?.url;
        if (url?.startsWith('https://www.openstreetmap.org/')) {
          const link = document.createElement('a');link.href=url;link.textContent='Открыть источник';link.target='_blank';link.rel='noopener noreferrer';
          content.append(document.createElement('br'),link);
        }
        const coordinates = object.geometry?.type === 'Point' ? object.geometry.coordinates : object.properties?.center;
        if (coordinates) new maplibregl.Popup().setLngLat(coordinates).setDOMContent(content).addTo(instance);
      },
      onError: () => setError('Не удалось отобразить один из слоёв карты.'),
    });
    overlay.current = deck;
    instance.addControl(deck);
    instance.on('load', () => setReady(true));
    instance.on('move', () => setViewRevision(value => value + 1));
    instance.on('error', () => setError('Часть карты не загрузилась. Попробуйте изменить масштаб.'));
    instance.on('click', e => {
      selection.current?.remove();
      selection.current = new maplibregl.Marker({color:'#e15d37'}).setLngLat(e.lngLat).addTo(instance);
      selectHandler.current({lat:e.lngLat.lat, lon:e.lngLat.lng});
    });
    const resize = new ResizeObserver(() => instance.resize());resize.observe(container.current);
    return () => {resize.disconnect();instance.remove();};
  }, []);
  useEffect(() => {if (!selectedPoint) selection.current?.remove();}, [selectedPoint]);
  useEffect(() => {
    if (!ready) return;
    const occupied = [];
    const metrics = document.createElement('canvas').getContext('2d');
    metrics.font = '600 13px Arial, sans-serif';
    const deckLayers = Object.values(layers).filter(l => l.visible).flatMap(layer => {
      const color = rgb(layer.color);
      const geometry = layer.kind === 'cones' ? new SimpleMeshLayer({
        id:layer.id, data:layer.data.features, mesh, pickable:true,
        getPosition:f => [...f.geometry.coordinates.slice(0,2), map.current.queryTerrainElevation(f.geometry.coordinates) || 0],
        getScale:[layer.radius_m,layer.radius_m,layer.height_m], getColor:[...color,160],
        material:{ambient:0.6,diffuse:0.7,shininess:20},
      }) : new GeoJsonLayer({id:layer.id, data:layer.data, pickable:true, filled:true, stroked:true,
        getFillColor:[...color,100], getLineColor:f => transportColors[f.properties.mode] || color,
        getPointRadius:7, pointRadiusUnits:'pixels', getLineWidth:4, lineWidthUnits:'pixels',
        pointRadiusMinPixels:5, getElevation:0,
      });
      if (layer.show_labels === false) return [geometry];
      const labels = layer.data.features.filter(f =>
        typeof f.properties?.name === 'string' && f.properties.name.trim() &&
        (f.geometry?.type === 'Point' ||
          (!['LineString', 'MultiLineString'].includes(f.geometry?.type) && Array.isArray(f.properties?.center)))).filter(f => {
          const coordinates = f.geometry.type === 'Point' ? f.geometry.coordinates : f.properties.center;
          const point = map.current.project(coordinates.slice(0,2));
          const box = {left:point.x + 10, top:point.y - 10,
            right:point.x + 14 + metrics.measureText(f.properties.name.trim()).width, bottom:point.y + 10};
          const canvas = map.current.getCanvas();
          if (box.right < 0 || box.left > canvas.clientWidth || box.bottom < 0 || box.top > canvas.clientHeight) return false;
          if (occupied.some(other => box.left < other.right && box.right > other.left &&
            box.top < other.bottom && box.bottom > other.top)) return false;
          occupied.push(box);
          return true;
        });
      return [geometry, new TextLayer({
        id:`${layer.id}:labels`, data:labels, pickable:false,
        getPosition:f => {
          const coordinates = f.geometry.type === 'Point' ? f.geometry.coordinates : f.properties.center;
          return [...coordinates.slice(0,2), map.current.queryTerrainElevation(coordinates.slice(0,2)) || 0];
        },
        getText:f => f.properties.name.trim(),
        getSize:13, sizeUnits:'pixels', fontFamily:'Arial, sans-serif', fontWeight:600,
        characterSet:'auto', fontSettings:{sdf:true}, getColor:[36,55,47], outlineWidth:2, outlineColor:[255,255,255,255],
        getTextAnchor:'start', getAlignmentBaseline:'center', getPixelOffset:[12,0],
        billboard:true, parameters:{depthCompare:'always'},
      })];
    });
    overlay.current.setProps({layers:deckLayers});
  }, [layers, ready, terrain, viewRevision]);
  useEffect(() => {
    if (!ready || !camera?.layer_id || !layers[camera.layer_id]) return;
    const bounds = boundsFor(layers[camera.layer_id]);
    if (!bounds.isEmpty()) map.current.fitBounds(bounds, {padding:70, maxZoom:16, pitch:camera.pitch || 0, duration:900});
  }, [camera, ready]);
  useEffect(() => {
    if (!ready) return;
    const instance = map.current;
    if (terrain) {
      if (!instance.getSource('dem')) instance.addSource('dem', {type:'raster-dem',tileSize:256,maxzoom:15,encoding:'terrarium',
        tiles:[`${location.origin}/ui/tiles/terrain/{z}/{x}/{y}.png`],attribution:'Elevation: <a href="https://registry.opendata.aws/terrain-tiles/" target="_blank" rel="noopener">Terrain Tiles / Mapzen</a>'});
      instance.setTerrain({source:'dem',exaggeration:1});
      instance.easeTo({pitch:55});
    } else instance.setTerrain(null);
  }, [terrain, ready]);
  return <section className="map-panel">
    <div ref={container} className="map-canvas" aria-label="Карта Казани" />
    <div className="map-heading"><span className="eyebrow">РАБОЧАЯ КАРТА</span><h1>Казань</h1><span>Места, маршруты и ваши слои</span></div>
    <button className={'terrain-button '+(terrain?'active':'')} onClick={() => setTerrain(v => !v)}>◈ Рельеф {terrain?'включён':'выключен'}</button>
    <div className="layer-panel">
      <div className="layer-header"><strong>Слои карты</strong><span>{Object.keys(layers).length}</span></div>
      {!Object.keys(layers).length && <p>Попросите агента найти места или построить маршрут — они появятся здесь.</p>}
      {Object.values(layers).map(layer => <div className="layer-item" key={layer.id}>
        <input aria-label={`Показать ${layer.title}`} type="checkbox" checked={layer.visible} disabled={busy} onChange={() => onVisibility(layer)} />
        <span className="color-dot" style={{background:layer.color}} />
        <button className="layer-title" onClick={() => {
          const bounds=boundsFor(layer);if(!bounds.isEmpty()) map.current.fitBounds(bounds,{padding:70,maxZoom:16,pitch:layer.kind==='cones'?55:0});
        }}>{layer.title}{layer.illustrative && <small>Иллюстрация · не зона видимости</small>}</button>
        <button className="icon-button" disabled={busy} aria-label={`Удалить ${layer.title}`} onClick={() => onRemove(layer)}>×</button>
      </div>)}
      <div className="map-legend"><span>● Места</span><span>━ Транспорт</span><span style={{color:'#d97706'}}>━ Авто</span><span>━ Пешком — серым</span></div>
    </div>
    {error && <button className="map-error" onClick={() => setError('')}>{error} ×</button>}
  </section>;
}
