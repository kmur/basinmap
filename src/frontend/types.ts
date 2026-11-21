export interface ReleasePartitionSpec {
  path: string;
  sha256: string;
  bytes: number;
  min_zoom: number;
  max_zoom: number;
  bounds?: [number, number, number, number];
  tiles: number;
}

export interface ReleaseLayerSpec {
  format: string;
  source_layer?: string;
  path: string;
  geojson_path?: string;
  sha256: string;
  bytes: number;
  min_zoom: number;
  max_zoom: number;
  tiles: number;
  cache_control: string;
  partitions?: ReleasePartitionSpec[];
}

export interface ReleaseRasterSpec {
  id: string;
  format: string;
  tile_type: string;
  tile_size: number;
  path: string;
  min_zoom: number;
  max_zoom: number;
  attribution: string;
  partitions?: ReleasePartitionSpec[];
}

export interface BasinShardRef {
  hybas_id: string;
  path: string;
  sha256: string;
  bytes: number;
}

export interface ReleaseManifest {
  schema_version: string;
  current_version: string;
  previous_version: string | null;
  generated_at: string;
  provenance: {
    gbif_doi: string;
    gbif_download_key: string;
    gbif_download_created: string;
    citations: Array<{ citation: string; doi: string }>;
  };
  spatial_coverage: {
    crs: string;
    bbox: [number, number, number, number];
    center: [number, number];
  };
  temporal_coverage?: { min_year: number; max_year: number; undated_records: number };
  counts: {
    occurrences: number;
    species: number;
    basins: number;
  };
  attribution: string;
  licenses: string[];
  layers: {
    basins: ReleaseLayerSpec;
    occurrences: ReleaseLayerSpec;
    basin_shards_template: string;
    basin_shards: BasinShardRef[];
    basin_shards_format?: string;
    basin_shards_index?: string;
    basin_shards_count?: number;
    occurrence_bins_max_zoom?: number;
    rasters?: ReleaseRasterSpec[];
    snapshot_own_occurrence_ids?: string;
  };
  overlays: {
    approved_observations: {
      format: string;
      path: string;
      cache_control: string;
    };
  };
}

export interface BasinScalarProperties {
  hybas_id: string;
  pfaf_id: string;
  basin_name: string;
  sub_area_km2: number;
  up_area_km2: number;
  occurrence_count: number;
  species_count: number;
  threatened_count: number;
  min_year: number;
  max_year: number;
  shard_path: string;
}

export interface SpeciesSummary {
  scientificName: string;
  taxonKey: number;
  family: string;
  iucnRedListCategory: string;
  count: number;
  firstYear: number;
  lastYear: number;
}

export interface BasinShardPayload {
  release_version: string;
  basin: BasinScalarProperties;
  bbox: [number, number, number, number];
  species: SpeciesSummary[];
  temporal_histogram: Record<string, number>;
  basis_of_record: Record<string, number>;
  datasets: Array<{
    datasetKey: string;
    datasetName: string;
    license: string;
    count: number;
  }>;
  provenance: {
    gbif_doi: string;
    download_key: string;
  };
}

export interface OccurrenceProperties {
  occurrenceID: string;
  gbifID?: number;
  scientificName: string;
  taxonKey?: number;
  eventDate: string;
  year: number;
  basisOfRecord: string;
  hybas_id?: string;
  license: string;
  institutionCode?: string;
  provisional?: boolean;
}

export interface GeoJsonFeature {
  type: "Feature";
  id?: string | number;
  properties: Record<string, unknown>;
  geometry: {
    type: string;
    coordinates: unknown;
  };
}

export interface ApprovedOverlayFeatureCollection {
  type: "FeatureCollection";
  name?: string;
  features: GeoJsonFeature[];
}

export interface PMTilesHeaderInfo {
  version: number;
  minZoom: number;
  maxZoom: number;
  numAddressedTiles: number;
  bounds: [number, number, number, number];
  centerZoom: number;
  center: [number, number];
}

export interface ApiHealthResponse {
  status: string;
  service: string;
  environment: string;
  public_data_url: string;
  bindings: {
    d1: boolean;
    public_r2: boolean;
    intake_r2: boolean;
  };
  timestamp: string;
}

export interface ApiConfigResponse {
  public_data_url: string;
  manifest_path: string;
  approved_overlay_path: string;
  supported_licenses: string[];
  submissions_enabled: boolean;
}

/** Archives backing a layer: its CDN-size partitions, or the single archive (v0.1 manifests). */
export function layerPartitions(spec: ReleaseLayerSpec | ReleaseRasterSpec): ReleasePartitionSpec[] {
  if (spec.partitions && spec.partitions.length > 0) {
    return spec.partitions;
  }
  return [
    {
      path: spec.path,
      sha256: (spec as ReleaseLayerSpec).sha256 || "",
      bytes: (spec as ReleaseLayerSpec).bytes || 0,
      min_zoom: spec.min_zoom,
      max_zoom: spec.max_zoom,
      tiles: (spec as ReleaseLayerSpec).tiles || 0,
    },
  ];
}

/** Release-relative shard path for a basin (JSON shard, or a regional SQLite partition). */
export function basinShardPath(manifest: ReleaseManifest, hybasId: string, shardPath?: string): string {
  if (shardPath && !shardPath.startsWith("/")) {
    return `/releases/${manifest.current_version}/${shardPath}`;
  }
  return manifest.layers.basin_shards_template.replace("{hybas_id}", hybasId);
}

export function resolveDataUrl(baseUrl: string, path: string): string {
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  const trimmedBase = baseUrl.trim().replace(/\/+$/, "");
  if (!trimmedBase) {
    return cleanPath;
  }
  return `${trimmedBase}${cleanPath}`;
}

export function parsePmtilesHeaderBytes(buffer: ArrayBuffer): PMTilesHeaderInfo {
  if (buffer.byteLength < 127) {
    throw new Error(`PMTiles header requires 127 bytes, got ${buffer.byteLength}`);
  }
  const view = new DataView(buffer);
  const magic = String.fromCharCode(
    view.getUint8(0),
    view.getUint8(1),
    view.getUint8(2),
    view.getUint8(3),
    view.getUint8(4),
    view.getUint8(5),
    view.getUint8(6)
  );
  const version = view.getUint8(7);
  if (magic !== "PMTiles" || version !== 3) {
    throw new Error(`Invalid PMTiles header magic=${magic} version=${version}`);
  }

  return {
    version,
    numAddressedTiles: Number(view.getBigUint64(72, true)),
    minZoom: view.getUint8(100),
    maxZoom: view.getUint8(101),
    bounds: [
      view.getInt32(102, true) / 1e7,
      view.getInt32(106, true) / 1e7,
      view.getInt32(110, true) / 1e7,
      view.getInt32(114, true) / 1e7,
    ],
    centerZoom: view.getUint8(118),
    center: [
      view.getInt32(119, true) / 1e7,
      view.getInt32(123, true) / 1e7,
    ],
  };
}

export function deduplicateApprovedOverlay(
  overlayFeatures: GeoJsonFeature[],
  snapshotOccurrenceIds: Set<string>
): { uniqueFeatures: GeoJsonFeature[]; duplicateCount: number } {
  const uniqueFeatures: GeoJsonFeature[] = [];
  let duplicateCount = 0;

  for (const feat of overlayFeatures) {
    const props = feat.properties || {};
    const occId = typeof props.occurrenceID === "string" ? props.occurrenceID : "";
    if (occId && snapshotOccurrenceIds.has(occId)) {
      duplicateCount += 1;
      continue;
    }
    uniqueFeatures.push(feat);
  }

  return { uniqueFeatures, duplicateCount };
}
