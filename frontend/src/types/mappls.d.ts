/**
 * Ambient type declarations for the Mappls Interactive Maps SDK.
 * The SDK is loaded at runtime via a <script> CDN tag and attaches
 * itself to window.mappls (and the legacy window.MapmyIndia alias).
 */

interface MapplsMapOptions {
  center: [number, number];     // [longitude, latitude]
  zoom?: number;
  zoomControl?: boolean;
  hybrid?: boolean;
  search?: boolean;
  mapStyle?: string;            // "raster" | "vector" | custom style URL
  backgroundColor?: string;
}

interface MapplsLatLng {
  lat: number;
  lng: number;
}

interface MapplsMarkerOptions {
  map?: MapplsMap;
  position: [number, number] | MapplsLatLng;
  icon?: {
    url: string;
    width?: number;
    height?: number;
    offset?: [number, number];
  } | string;
  draggable?: boolean;
  title?: string;
  fitbounds?: boolean;
  fitboundOptions?: { padding: number };
  popupOptions?: { offset?: [number, number]; closeButton?: boolean };
  popupHtml?: string;
}

interface MapplsPolylineOptions {
  map: MapplsMap;
  path: [number, number][];
  strokeColor?: string;
  strokeOpacity?: number;
  strokeWeight?: number;
  fitbounds?: boolean;
}

interface MapplsCircleOptions {
  map: MapplsMap;
  center: [number, number];
  radius: number;              // metres
  strokeColor?: string;
  strokeOpacity?: number;
  strokeWeight?: number;
  fillColor?: string;
  fillOpacity?: number;
}

interface MapplsMarker {
  setPosition(position: [number, number] | MapplsLatLng): void;
  getPosition(): MapplsLatLng;
  setIcon(icon: MapplsMarkerOptions["icon"]): void;
  remove(): void;
  setMap(map: MapplsMap | null): void;
}

interface MapplsPolyline {
  setPath(path: [number, number][]): void;
  remove(): void;
  setMap(map: MapplsMap | null): void;
}

interface MapplsCircle {
  remove(): void;
  setMap(map: MapplsMap | null): void;
}

interface MapplsMap {
  setCenter(lngLat: [number, number]): void;
  setZoom(zoom: number): void;
  getCenter(): MapplsLatLng;
  getZoom(): number;
  on(event: string, handler: (e: unknown) => void): void;
  off(event: string, handler: (e: unknown) => void): void;
  remove(): void;
  resize(): void;
  fitBounds(bounds: [[number, number], [number, number]], options?: { padding: number }): void;
}

interface MapplsSDK {
  Map: new (container: string | HTMLElement, options: MapplsMapOptions) => MapplsMap;
  Marker: new (options: MapplsMarkerOptions) => MapplsMarker;
  Polyline: new (options: MapplsPolylineOptions) => MapplsPolyline;
  Circle: new (options: MapplsCircleOptions) => MapplsCircle;
  event: {
    addListener(
      target: MapplsMap | MapplsMarker,
      event: string,
      handler: (e: unknown) => void
    ): void;
    removeListener(
      target: MapplsMap | MapplsMarker,
      event: string,
      handler: (e: unknown) => void
    ): void;
  };
}

declare global {
  interface Window {
    mappls: MapplsSDK;
    MapmyIndia: MapplsSDK;   // legacy alias
    mapplsgl: MapplsSDK;     // GL variant
  }
}

export {};
