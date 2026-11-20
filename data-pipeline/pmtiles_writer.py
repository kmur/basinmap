#!/usr/bin/env python3
"""Pure-Python PMTiles v3 and Mapbox Vector Tile (MVT v2.1) writer & inspector.

Implements:
  - PMTiles v3 Hilbert curve tile ID conversion (zxy <-> tile_id)
  - MVT v2.1 protobuf encoder and decoder (POINT and POLYGON layers)
  - PMTiles v3 archive builder (127-byte header, gzipped metadata & root dir)
  - PMTiles v3 inspector and byte-level tile reader for automated verification
"""

from __future__ import annotations

import gzip
import json
import math
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

PMTILES_MAGIC = b"PMTiles\x03"
PMTILES_HEADER_STRUCT = struct.Struct("<8s11Q6B4iB2i")
assert PMTILES_HEADER_STRUCT.size == 127

COMPRESSION_NONE = 1
COMPRESSION_GZIP = 2
TILE_TYPE_MVT = 1
TILE_TYPE_PNG = 2
TILE_TYPE_JPEG = 3
TILE_TYPE_WEBP = 4

# PMTiles v3 spec: clients fetch the first 16 KiB, which must contain the
# 127-byte header plus the complete (compressed) root directory.
ROOT_DIRECTORY_BUDGET = 16384 - 127

GEOM_UNKNOWN = 0
GEOM_POINT = 1
GEOM_LINESTRING = 2
GEOM_POLYGON = 3

CMD_MOVETO = 1
CMD_LINETO = 2
CMD_CLOSEPATH = 7

DEFAULT_EXTENT = 4096


# ---------------------------------------------------------------------------
# Hilbert curve mapping (PMTiles v3 spec)
# ---------------------------------------------------------------------------

def _rot(n: int, x: int, y: int, rx: int, ry: int) -> Tuple[int, int]:
    if ry == 0:
        if rx == 1:
            x = n - 1 - x
            y = n - 1 - y
        x, y = y, x
    return x, y


def zxy_to_tile_id(z: int, x: int, y: int) -> int:
    """Convert Web Mercator (z, x, y) to PMTiles v3 Hilbert tile ID."""
    if z < 0:
        raise ValueError(f"Zoom must be non-negative, got {z}")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise ValueError(f"Tile ({z}, {x}, {y}) out of range for zoom {z}")
    if z == 0:
        return 0
    acc = ((1 << (2 * z)) - 1) // 3
    d = 0
    s = n >> 1
    cx, cy = x, y
    while s > 0:
        rx = 1 if (cx & s) > 0 else 0
        ry = 1 if (cy & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        cx, cy = _rot(n, cx, cy, rx, ry)
        s >>= 1
    return acc + d


def tile_id_to_zxy(tile_id: int) -> Tuple[int, int, int]:
    """Convert PMTiles v3 Hilbert tile ID back to (z, x, y)."""
    if tile_id < 0:
        raise ValueError(f"tile_id must be non-negative, got {tile_id}")
    if tile_id == 0:
        return (0, 0, 0)
    z = 0
    acc = 0
    while True:
        num_tiles = 1 << (2 * z)
        if tile_id < acc + num_tiles:
            break
        acc += num_tiles
        z += 1
    d = tile_id - acc
    n = 1 << z
    t = d
    x = 0
    y = 0
    s = 1
    while s < n:
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        x, y = _rot(s, x, y, rx, ry)
        x += s * rx
        y += s * ry
        t //= 4
        s *= 2
    return (z, x, y)


# ---------------------------------------------------------------------------
# Protobuf & MVT v2.1 encoding / decoding
# ---------------------------------------------------------------------------

def _encode_varint(value: int) -> bytes:
    if value < 0:
        # Two's complement 64-bit representation for negative int64
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _decode_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("Truncated varint in buffer")


def _zigzag_encode(n: int) -> int:
    return (2 * n) if n >= 0 else (-2 * n - 1)


def _zigzag_decode(u: int) -> int:
    return (u >> 1) ^ -(u & 1)


def _pb_tag(field_num: int, wire_type: int) -> bytes:
    return _encode_varint((field_num << 3) | wire_type)


def _pb_bytes(field_num: int, payload: bytes) -> bytes:
    return _pb_tag(field_num, 2) + _encode_varint(len(payload)) + payload


def _pb_packed_varints(field_num: int, values: Sequence[int]) -> bytes:
    body = b"".join(_encode_varint(v) for v in values)
    return _pb_bytes(field_num, body)


def _encode_mvt_value(val: Any) -> bytes:
    if isinstance(val, bool):
        return _pb_tag(7, 0) + _encode_varint(1 if val else 0)
    if isinstance(val, int):
        if val >= 0:
            return _pb_tag(4, 0) + _encode_varint(val)
        return _pb_tag(6, 0) + _encode_varint(_zigzag_encode(val))
    if isinstance(val, float):
        return _pb_tag(3, 1) + struct.pack("<d", val)
    return _pb_bytes(1, str(val).encode("utf-8"))


def _encode_point_geometry(points: Sequence[Tuple[int, int]]) -> List[int]:
    if not points:
        return []
    cmds: List[int] = [(CMD_MOVETO & 0x7) | (len(points) << 3)]
    cx, cy = 0, 0
    for px, py in points:
        cmds.append(_zigzag_encode(px - cx))
        cmds.append(_zigzag_encode(py - cy))
        cx, cy = px, py
    return cmds


def _signed_area(ring: Sequence[Tuple[int, int]]) -> int:
    area = 0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area


def _encode_polygon_geometry(rings: Sequence[Sequence[Tuple[int, int]]]) -> List[int]:
    cmds: List[int] = []
    cx, cy = 0, 0
    for idx, raw_ring in enumerate(rings):
        if len(raw_ring) < 3:
            continue
        ring = list(raw_ring)
        area = _signed_area(ring)
        if area == 0:
            continue
        # MVT spec: exterior ring (idx == 0) clockwise in screen coords (area > 0)
        if idx == 0 and area < 0:
            ring.reverse()
        elif idx > 0 and area > 0:
            ring.reverse()

        x0, y0 = ring[0]
        cmds.append((CMD_MOVETO & 0x7) | (1 << 3))
        cmds.append(_zigzag_encode(x0 - cx))
        cmds.append(_zigzag_encode(y0 - cy))
        cx, cy = x0, y0

        line_pts = ring[1:]
        cmds.append((CMD_LINETO & 0x7) | (len(line_pts) << 3))
        for px, py in line_pts:
            cmds.append(_zigzag_encode(px - cx))
            cmds.append(_zigzag_encode(py - cy))
            cx, cy = px, py

        cmds.append((CMD_CLOSEPATH & 0x7) | (1 << 3))
    return cmds


def encode_mvt_tile(
    layers: Dict[str, List[Dict[str, Any]]],
    extent: int = DEFAULT_EXTENT,
) -> bytes:
    """Encode a dict of {layer_name: [feature_dicts]} into MVT v2.1 bytes.

    Each feature dict must contain:
      - 'geom_type': GEOM_POINT or GEOM_POLYGON
      - 'geometry': list of (x, y) for points, or list of rings [[(x, y), ...]] for polygons
      - 'properties': dict of scalar property values
      - 'id' (optional): non-negative integer feature id
    """
    tile_chunks: List[bytes] = []
    for layer_name, features in layers.items():
        keys: List[str] = []
        key_index: Dict[str, int] = {}
        values_enc: List[bytes] = []
        value_index: Dict[Tuple[str, Any], int] = {}

        feature_bytes_list: List[bytes] = []
        for feat in features:
            geom_type = feat["geom_type"]
            if geom_type == GEOM_POINT:
                geom_cmds = _encode_point_geometry(feat["geometry"])
            elif geom_type == GEOM_POLYGON:
                geom_cmds = _encode_polygon_geometry(feat["geometry"])
            else:
                raise ValueError(f"Unsupported MVT geom_type: {geom_type}")

            if not geom_cmds:
                continue

            tags: List[int] = []
            for k, v in feat.get("properties", {}).items():
                if v is None:
                    continue
                if k not in key_index:
                    key_index[k] = len(keys)
                    keys.append(k)
                val_key = (type(v).__name__, v)
                if val_key not in value_index:
                    value_index[val_key] = len(values_enc)
                    values_enc.append(_encode_mvt_value(v))
                tags.append(key_index[k])
                tags.append(value_index[val_key])

            feat_parts: List[bytes] = []
            if feat.get("id") is not None:
                feat_parts.append(_pb_tag(1, 0) + _encode_varint(int(feat["id"])))
            if tags:
                feat_parts.append(_pb_packed_varints(2, tags))
            feat_parts.append(_pb_tag(3, 0) + _encode_varint(geom_type))
            feat_parts.append(_pb_packed_varints(4, geom_cmds))
            feature_bytes_list.append(_pb_bytes(2, b"".join(feat_parts)))

        layer_parts: List[bytes] = [
            _pb_tag(15, 0) + _encode_varint(2),  # MVT v2
            _pb_bytes(1, layer_name.encode("utf-8")),
        ]
        layer_parts.extend(feature_bytes_list)
        for k in keys:
            layer_parts.append(_pb_bytes(3, k.encode("utf-8")))
        for v_enc in values_enc:
            layer_parts.append(_pb_bytes(4, v_enc))
        layer_parts.append(_pb_tag(5, 0) + _encode_varint(extent))
        tile_chunks.append(_pb_bytes(3, b"".join(layer_parts)))

    return b"".join(tile_chunks)


def _read_pb_fields(buf: bytes) -> List[Tuple[int, int, Any]]:
    pos = 0
    fields: List[Tuple[int, int, Any]] = []
    while pos < len(buf):
        tag, pos = _decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:
            val, pos = _decode_varint(buf, pos)
            fields.append((field_num, wire_type, val))
        elif wire_type == 1:
            val_bytes = buf[pos : pos + 8]
            pos += 8
            fields.append((field_num, wire_type, val_bytes))
        elif wire_type == 2:
            length, pos = _decode_varint(buf, pos)
            val_bytes = buf[pos : pos + length]
            pos += length
            fields.append((field_num, wire_type, val_bytes))
        elif wire_type == 5:
            val_bytes = buf[pos : pos + 4]
            pos += 4
            fields.append((field_num, wire_type, val_bytes))
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")
    return fields


def _decode_mvt_value(buf: bytes) -> Any:
    for fnum, _, val in _read_pb_fields(buf):
        if fnum == 1:
            return val.decode("utf-8")
        if fnum == 2:
            return struct.unpack("<f", val)[0]
        if fnum == 3:
            return struct.unpack("<d", val)[0]
        if fnum == 4 or fnum == 5:
            return int(val)
        if fnum == 6:
            return _zigzag_decode(int(val))
        if fnum == 7:
            return bool(val)
    return None


def decode_mvt_tile(tile_bytes: bytes) -> Dict[str, Dict[str, Any]]:
    """Decode MVT v2.1 bytes into {layer_name: {'extent': int, 'features': [...]}}."""
    layers_out: Dict[str, Dict[str, Any]] = {}
    for fnum, _, layer_raw in _read_pb_fields(tile_bytes):
        if fnum != 3:
            continue
        name = ""
        version = 1
        extent = DEFAULT_EXTENT
        keys: List[str] = []
        values: List[Any] = []
        raw_features: List[bytes] = []
        for lfnum, _, lval in _read_pb_fields(layer_raw):
            if lfnum == 1:
                name = lval.decode("utf-8")
            elif lfnum == 2:
                raw_features.append(lval)
            elif lfnum == 3:
                keys.append(lval.decode("utf-8"))
            elif lfnum == 4:
                values.append(_decode_mvt_value(lval))
            elif lfnum == 5:
                extent = int(lval)
            elif lfnum == 15:
                version = int(lval)

        features_out: List[Dict[str, Any]] = []
        for fraw in raw_features:
            fid: Optional[int] = None
            tags: List[int] = []
            geom_type = GEOM_UNKNOWN
            geom: List[int] = []
            for ffnum, _, fval in _read_pb_fields(fraw):
                if ffnum == 1:
                    fid = int(fval)
                elif ffnum == 2:
                    p = 0
                    while p < len(fval):
                        t, p = _decode_varint(fval, p)
                        tags.append(t)
                elif ffnum == 3:
                    geom_type = int(fval)
                elif ffnum == 4:
                    p = 0
                    while p < len(fval):
                        g, p = _decode_varint(fval, p)
                        geom.append(g)
            props = {}
            for i in range(0, len(tags) - 1, 2):
                props[keys[tags[i]]] = values[tags[i + 1]]
            features_out.append(
                {
                    "id": fid,
                    "geom_type": geom_type,
                    "properties": props,
                    "geometry": geom,
                }
            )
        layers_out[name] = {
            "version": version,
            "extent": extent,
            "features": features_out,
        }
    return layers_out


# ---------------------------------------------------------------------------
# Web Mercator projection & polygon clipping helpers
# ---------------------------------------------------------------------------

def lon_to_tile_x(lon: float, zoom: int) -> float:
    return ((lon + 180.0) / 360.0) * (1 << zoom)


def lat_to_tile_y(lat: float, zoom: int) -> float:
    clamped = max(-85.05112878, min(85.05112878, lat))
    rad = math.radians(clamped)
    return ((1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0) * (1 << zoom)


def clip_ring_to_bbox(
    ring: Sequence[Tuple[float, float]],
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
) -> List[Tuple[float, float]]:
    """Sutherland-Hodgman clip of a closed/open polygon ring to a rectangle."""
    if not ring:
        return []
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]

    def clip_edge(
        poly: List[Tuple[float, float]],
        inside_fn: Any,
        intersect_fn: Any,
    ) -> List[Tuple[float, float]]:
        if not poly:
            return []
        output: List[Tuple[float, float]] = []
        prev = poly[-1]
        prev_in = inside_fn(prev)
        for curr in poly:
            curr_in = inside_fn(curr)
            if curr_in:
                if not prev_in:
                    output.append(intersect_fn(prev, curr))
                output.append(curr)
            elif prev_in:
                output.append(intersect_fn(prev, curr))
            prev = curr
            prev_in = curr_in
        return output

    def intersect_vertical(p1: Tuple[float, float], p2: Tuple[float, float], x_edge: float) -> Tuple[float, float]:
        x1, y1 = p1
        x2, y2 = p2
        dx = x2 - x1
        if abs(dx) < 1e-12:
            return (x_edge, y1)
        t = (x_edge - x1) / dx
        return (x_edge, y1 + t * (y2 - y1))

    def intersect_horizontal(p1: Tuple[float, float], p2: Tuple[float, float], y_edge: float) -> Tuple[float, float]:
        x1, y1 = p1
        x2, y2 = p2
        dy = y2 - y1
        if abs(dy) < 1e-12:
            return (x1, y_edge)
        t = (y_edge - y1) / dy
        return (x1 + t * (x2 - x1), y_edge)

    pts = clip_edge(pts, lambda p: p[0] >= min_x, lambda a, b: intersect_vertical(a, b, min_x))
    pts = clip_edge(pts, lambda p: p[0] <= max_x, lambda a, b: intersect_vertical(a, b, max_x))
    pts = clip_edge(pts, lambda p: p[1] >= min_y, lambda a, b: intersect_horizontal(a, b, min_y))
    pts = clip_edge(pts, lambda p: p[1] <= max_y, lambda a, b: intersect_horizontal(a, b, max_y))
    return pts


def project_polygon_to_tile(
    lon_lat_ring: Sequence[Sequence[float]],
    zoom: int,
    tile_x: int,
    tile_y: int,
    extent: int = DEFAULT_EXTENT,
    buffer: int = 64,
) -> Optional[List[Tuple[int, int]]]:
    """Project a lon/lat ring to tile screen integer coordinates and clip with buffer."""
    local_float = [
        (
            (lon_to_tile_x(pt[0], zoom) - tile_x) * extent,
            (lat_to_tile_y(pt[1], zoom) - tile_y) * extent,
        )
        for pt in lon_lat_ring
    ]
    clipped = clip_ring_to_bbox(
        local_float,
        -float(buffer),
        -float(buffer),
        float(extent + buffer),
        float(extent + buffer),
    )
    if len(clipped) < 3:
        return None
    rounded: List[Tuple[int, int]] = []
    for px, py in clipped:
        ix, iy = round(px), round(py)
        if not rounded or rounded[-1] != (ix, iy):
            rounded.append((ix, iy))
    if len(rounded) > 1 and rounded[0] == rounded[-1]:
        rounded.pop()
    if len(rounded) < 3 or _signed_area(rounded) == 0:
        return None
    return rounded


# ---------------------------------------------------------------------------
# PMTiles v3 archive serialization & inspection
# ---------------------------------------------------------------------------

def _serialize_directory(entries: Sequence[Tuple[int, int, int, int]]) -> bytes:
    """Serialize PMTiles v3 directory entries: (tile_id, offset, length, run_length)."""
    n = len(entries)
    chunks: List[bytes] = [_encode_varint(n)]

    # 1. tile_id deltas
    last_id = 0
    for tile_id, _, _, _ in entries:
        chunks.append(_encode_varint(tile_id - last_id))
        last_id = tile_id

    # 2. run_lengths
    for _, _, _, run_length in entries:
        chunks.append(_encode_varint(run_length))

    # 3. lengths
    for _, _, length, _ in entries:
        chunks.append(_encode_varint(length))

    # 4. offsets (0 when contiguous with previous entry; otherwise offset + 1)
    for idx, (_, offset, _, _) in enumerate(entries):
        if idx > 0 and offset == entries[idx - 1][1] + entries[idx - 1][2]:
            chunks.append(_encode_varint(0))
        else:
            chunks.append(_encode_varint(offset + 1))

    return b"".join(chunks)


def _deserialize_directory(raw_dir: bytes) -> List[Tuple[int, int, int, int]]:
    pos = 0
    n, pos = _decode_varint(raw_dir, pos)

    tile_ids: List[int] = []
    last_id = 0
    for _ in range(n):
        delta, pos = _decode_varint(raw_dir, pos)
        last_id += delta
        tile_ids.append(last_id)

    run_lengths: List[int] = []
    for _ in range(n):
        rl, pos = _decode_varint(raw_dir, pos)
        run_lengths.append(rl)

    lengths: List[int] = []
    for _ in range(n):
        ln, pos = _decode_varint(raw_dir, pos)
        lengths.append(ln)

    offsets: List[int] = []
    for idx in range(n):
        val, pos = _decode_varint(raw_dir, pos)
        if val == 0 and idx > 0:
            offsets.append(offsets[idx - 1] + lengths[idx - 1])
        else:
            offsets.append(val - 1)

    return [
        (tile_ids[i], offsets[i], lengths[i], run_lengths[i])
        for i in range(n)
    ]


def write_pmtiles(
    output_path: Path,
    tiles_by_zxy: Dict[Tuple[int, int, int], bytes],
    metadata: Dict[str, Any],
    min_zoom: int,
    max_zoom: int,
    bounds: Sequence[float],
    center: Sequence[float],
    center_zoom: Optional[int] = None,
    tile_type: int = TILE_TYPE_MVT,
    tile_compression: Optional[int] = None,
    tiles_precompressed: bool = False,
) -> Dict[str, int]:
    """Write a PMTiles v3 file from {(z, x, y): raw_tile_bytes} and return archive stats.

    Vector (MVT) tiles are gzip-compressed. Raster tiles (PNG/JPEG/WebP) are stored
    as-is (``COMPRESSION_NONE``) because the image codec already compresses them.
    Leaf directories are emitted automatically when the root directory would not
    fit in the first 16 KiB of the archive.
    """
    if not tiles_by_zxy:
        raise ValueError("Cannot write PMTiles archive with zero tiles")
    if tile_compression is None:
        tile_compression = COMPRESSION_GZIP if tile_type == TILE_TYPE_MVT else COMPRESSION_NONE

    sorted_items = sorted(
        ((zxy_to_tile_id(z, x, y), raw_bytes) for (z, x, y), raw_bytes in tiles_by_zxy.items()),
        key=lambda item: item[0],
    )

    tile_blobs: List[bytes] = []
    payload_cache: Dict[bytes, Tuple[int, int]] = {}
    tile_data_len = 0
    entries: List[Tuple[int, int, int, int]] = []

    for tile_id, raw_bytes in sorted_items:
        compressed = (
            gzip.compress(raw_bytes, mtime=0)
            if tile_compression == COMPRESSION_GZIP and not tiles_precompressed
            else raw_bytes
        )
        if compressed in payload_cache:
            offset, length = payload_cache[compressed]
        else:
            offset = tile_data_len
            length = len(compressed)
            payload_cache[compressed] = (offset, length)
            tile_blobs.append(compressed)
            tile_data_len += length

        if entries:
            prev_tid, prev_off, prev_len, prev_rl = entries[-1]
            if prev_tid + prev_rl == tile_id and prev_off == offset and prev_len == length:
                entries[-1] = (prev_tid, prev_off, prev_len, prev_rl + 1)
                continue
        entries.append((tile_id, offset, length, 1))

    root_dir_bytes, leaf_dir_blob, num_leaves = build_directories(entries)
    json_meta_bytes = gzip.compress(
        json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        mtime=0,
    )
    tile_data_bytes = b"".join(tile_blobs)

    root_dir_offset = 127
    json_metadata_offset = root_dir_offset + len(root_dir_bytes)
    leaf_dirs_offset = json_metadata_offset + len(json_meta_bytes)
    leaf_dirs_bytes = len(leaf_dir_blob)
    tile_data_offset = leaf_dirs_offset + leaf_dirs_bytes

    min_lon, min_lat, max_lon, max_lat = bounds
    c_lon, c_lat = center
    c_zoom = center_zoom if center_zoom is not None else min(max_zoom, max(min_zoom, (min_zoom + max_zoom) // 2))

    header = PMTILES_HEADER_STRUCT.pack(
        PMTILES_MAGIC,
        root_dir_offset,
        len(root_dir_bytes),
        json_metadata_offset,
        len(json_meta_bytes),
        leaf_dirs_offset,
        leaf_dirs_bytes,
        tile_data_offset,
        len(tile_data_bytes),
        len(sorted_items),       # num_addressed_tiles
        len(entries),            # num_tile_entries
        len(payload_cache),      # num_tile_contents
        1,                       # clustered
        COMPRESSION_GZIP,        # internal_compression
        tile_compression,        # tile_compression
        tile_type,               # tile_type
        min_zoom,
        max_zoom,
        round(min_lon * 1e7),
        round(min_lat * 1e7),
        round(max_lon * 1e7),
        round(max_lat * 1e7),
        c_zoom,
        round(c_lon * 1e7),
        round(c_lat * 1e7),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as f:
        f.write(header)
        f.write(root_dir_bytes)
        f.write(json_meta_bytes)
        f.write(leaf_dir_blob)
        f.write(tile_data_bytes)

    return {
        "total_bytes": 127
        + len(root_dir_bytes)
        + len(json_meta_bytes)
        + leaf_dirs_bytes
        + len(tile_data_bytes),
        "num_addressed_tiles": len(sorted_items),
        "num_tile_entries": len(entries),
        "num_tile_contents": len(payload_cache),
        "num_leaf_directories": num_leaves,
        "root_dir_bytes": len(root_dir_bytes),
    }


def build_directories(
    entries: Sequence[Tuple[int, int, int, int]],
) -> Tuple[bytes, bytes, int]:
    """Return (root_dir_gz, leaf_dirs_blob, num_leaves) honoring the 16 KiB root budget."""
    root = gzip.compress(_serialize_directory(entries), mtime=0)
    if len(root) <= ROOT_DIRECTORY_BUDGET:
        return root, b"", 0

    leaf_size = 4096
    while True:
        root_entries: List[Tuple[int, int, int, int]] = []
        leaf_chunks: List[bytes] = []
        leaf_len = 0
        for i in range(0, len(entries), leaf_size):
            chunk = entries[i : i + leaf_size]
            leaf_gz = gzip.compress(_serialize_directory(chunk), mtime=0)
            # run_length == 0 marks an entry that points to a leaf directory.
            root_entries.append((chunk[0][0], leaf_len, len(leaf_gz), 0))
            leaf_chunks.append(leaf_gz)
            leaf_len += len(leaf_gz)
        root = gzip.compress(_serialize_directory(root_entries), mtime=0)
        if len(root) <= ROOT_DIRECTORY_BUDGET:
            return root, b"".join(leaf_chunks), len(leaf_chunks)
        leaf_size *= 2


def iter_directory_entries(
    read_range: Any,
    header: Dict[str, Any],
) -> List[Tuple[int, int, int, int]]:
    """Return all tile entries (expanding leaf directories) via read_range(offset, length)."""
    root_raw = read_range(header["root_dir_offset"], header["root_dir_bytes"])
    out: List[Tuple[int, int, int, int]] = []
    for tile_id, offset, length, run_length in _deserialize_directory(gzip.decompress(root_raw)):
        if run_length == 0:
            leaf_raw = read_range(header["leaf_dirs_offset"] + offset, length)
            out.extend(_deserialize_directory(gzip.decompress(leaf_raw)))
        else:
            out.append((tile_id, offset, length, run_length))
    return out


def _find_entry(
    entries: Sequence[Tuple[int, int, int, int]], tile_id: int
) -> Optional[Tuple[int, int, int, int]]:
    lo, hi = 0, len(entries) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if entries[mid][0] < tile_id:
            lo = mid + 1
        elif entries[mid][0] > tile_id:
            hi = mid - 1
        else:
            return entries[mid]
    if hi >= 0:
        cand = entries[hi]
        if cand[3] == 0 or tile_id - cand[0] < cand[3]:
            return cand
    return None


class PMTilesRangeReader:
    """Minimal PMTiles v3 client mirroring pmtiles.js request behavior.

    ``fetch(offset, length) -> bytes`` performs one byte-range read. Like pmtiles.js,
    the first read fetches 16 KiB (header + root directory); leaf directories and
    tiles are fetched individually and leaf directories are cached.
    """

    def __init__(self, fetch: Any) -> None:
        self._fetch = fetch
        self._initial = b""
        self.header: Dict[str, Any] = {}
        self._root: List[Tuple[int, int, int, int]] = []
        self._leaf_cache: Dict[int, List[Tuple[int, int, int, int]]] = {}

    def open(self) -> Dict[str, Any]:
        self._initial = self._fetch(0, 16384)
        self.header = parse_pmtiles_header(self._initial[:127])
        rd_off, rd_len = self.header["root_dir_offset"], self.header["root_dir_bytes"]
        if rd_off + rd_len > len(self._initial):
            raise ValueError("Root directory exceeds the first 16 KiB (spec violation)")
        self._root = _deserialize_directory(gzip.decompress(self._initial[rd_off : rd_off + rd_len]))
        return self.header

    def get_tile(self, z: int, x: int, y: int) -> Optional[bytes]:
        if not self.header:
            self.open()
        tile_id = zxy_to_tile_id(z, x, y)
        entries = self._root
        for _ in range(4):  # spec allows bounded directory depth
            entry = _find_entry(entries, tile_id)
            if entry is None:
                return None
            _, offset, length, run_length = entry
            if run_length > 0:
                return self._fetch(self.header["tile_data_offset"] + offset, length)
            leaf_off = self.header["leaf_dirs_offset"] + offset
            if leaf_off not in self._leaf_cache:
                raw = self._fetch(leaf_off, length)
                self._leaf_cache[leaf_off] = _deserialize_directory(gzip.decompress(raw))
            entries = self._leaf_cache[leaf_off]
        return None


def parse_pmtiles_header(header_bytes: bytes) -> Dict[str, Any]:
    """Parse and validate a 127-byte PMTiles v3 header."""
    if len(header_bytes) < 127:
        raise ValueError(f"PMTiles header requires 127 bytes, got {len(header_bytes)}")
    fields = PMTILES_HEADER_STRUCT.unpack(header_bytes[:127])
    magic = fields[0]
    if magic != PMTILES_MAGIC:
        raise ValueError(f"Invalid PMTiles magic/version: {magic!r}")
    return {
        "magic": magic.decode("ascii", errors="replace"),
        "version": 3,
        "root_dir_offset": fields[1],
        "root_dir_bytes": fields[2],
        "json_metadata_offset": fields[3],
        "json_metadata_bytes": fields[4],
        "leaf_dirs_offset": fields[5],
        "leaf_dirs_bytes": fields[6],
        "tile_data_offset": fields[7],
        "tile_data_bytes": fields[8],
        "num_addressed_tiles": fields[9],
        "num_tile_entries": fields[10],
        "num_tile_contents": fields[11],
        "clustered": bool(fields[12]),
        "internal_compression": fields[13],
        "tile_compression": fields[14],
        "tile_type": fields[15],
        "min_zoom": fields[16],
        "max_zoom": fields[17],
        "bounds": [fields[18] / 1e7, fields[19] / 1e7, fields[20] / 1e7, fields[21] / 1e7],
        "center_zoom": fields[22],
        "center": [fields[23] / 1e7, fields[24] / 1e7],
    }


def inspect_pmtiles(source: Union[Path, bytes]) -> Dict[str, Any]:
    """Read header, root directory, metadata, and verify tile slices in a PMTiles v3 archive."""
    raw = source.read_bytes() if isinstance(source, Path) else source
    header = parse_pmtiles_header(raw[:127])

    entries = iter_directory_entries(lambda off, ln: raw[off : off + ln], header)

    md_start = header["json_metadata_offset"]
    md_end = md_start + header["json_metadata_bytes"]
    metadata = json.loads(gzip.decompress(raw[md_start:md_end]).decode("utf-8"))

    td_start = header["tile_data_offset"]
    tiles_by_zxy: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    for tile_id, offset, length, run_length in entries:
        slice_bytes = raw[td_start + offset : td_start + offset + length]
        if len(slice_bytes) != length:
            raise ValueError(f"Tile slice truncated for tile_id={tile_id}")
        if header["tile_type"] != TILE_TYPE_MVT:
            decoded = {"_raster": {"bytes": length, "magic": slice_bytes[:12]}}
        else:
            mvt_raw = (
                gzip.decompress(slice_bytes)
                if header["tile_compression"] == COMPRESSION_GZIP
                else slice_bytes
            )
            decoded = decode_mvt_tile(mvt_raw)
        for r in range(run_length):
            zxy = tile_id_to_zxy(tile_id + r)
            tiles_by_zxy[zxy] = decoded

    return {
        "header": header,
        "metadata": metadata,
        "entries_count": len(entries),
        "tiles": tiles_by_zxy,
    }
