/**
 * Media helpers shared by the upload and review routes (mirrors scripts/backend_engine.py):
 * magic-byte type detection, private EXIF extraction for reviewers, and JPEG metadata stripping.
 */

export interface ExifSummary {
  gps_latitude: number | null;
  gps_longitude: number | null;
  datetime: string | null;
  orientation: number;
}

export function detectMagicMime(head: Uint8Array): string | null {
  if (head.byteLength < 12) return null;
  if (head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) return "image/jpeg";
  const png = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
  if (png.every((b, i) => head[i] === b)) return "image/png";
  const ascii = (a: number, b: number) => String.fromCharCode(...Array.from(head.slice(a, b)));
  if (ascii(0, 4) === "RIFF" && ascii(8, 12) === "WEBP") return "image/webp";
  if (ascii(4, 8) === "ftyp" && ["heic", "heix", "hevc", "mif1", "msf1"].includes(ascii(8, 12))) {
    return "image/heic";
  }
  return null;
}

const TYPE_SIZES: Record<number, number> = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8 };

function readIfdValue(view: DataView, little: boolean, ftype: number, count: number, entryPos: number): unknown {
  const size = (TYPE_SIZES[ftype] || 1) * count;
  let off = entryPos + 8;
  if (size > 4) {
    off = view.getUint32(entryPos + 8, little);
    if (off + size > view.byteLength) return null;
  }
  if (ftype === 2) {
    let s = "";
    for (let i = 0; i < count; i++) {
      const c = view.getUint8(off + i);
      if (c === 0) break;
      s += String.fromCharCode(c);
    }
    return s.trim();
  }
  if (ftype === 3) return count === 1 ? view.getUint16(off, little) : null;
  if (ftype === 4) return count === 1 ? view.getUint32(off, little) : null;
  if (ftype === 5) {
    const vals: number[] = [];
    for (let i = 0; i < count; i++) {
      const num = view.getUint32(off + i * 8, little);
      const den = view.getUint32(off + i * 8 + 4, little);
      vals.push(den ? num / den : 0);
    }
    return count === 1 ? vals[0] : vals;
  }
  return null;
}

function parseIfd(view: DataView, little: boolean, ifdOffset: number): Map<number, unknown> {
  const tags = new Map<number, unknown>();
  if (ifdOffset + 2 > view.byteLength) return tags;
  const n = view.getUint16(ifdOffset, little);
  for (let i = 0; i < n; i++) {
    const pos = ifdOffset + 2 + i * 12;
    if (pos + 12 > view.byteLength) break;
    tags.set(view.getUint16(pos, little), readIfdValue(view, little, view.getUint16(pos + 2, little), view.getUint32(pos + 4, little), pos));
  }
  return tags;
}

/** GPS position, capture time, and orientation from a JPEG APP1 EXIF block (private, reviewer-only). */
export function extractExif(bytes: Uint8Array, mime: string): ExifSummary {
  const out: ExifSummary = { gps_latitude: null, gps_longitude: null, datetime: null, orientation: 1 };
  if (mime !== "image/jpeg" || bytes.byteLength < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return out;
  let pos = 2;
  while (pos + 4 <= bytes.byteLength && bytes[pos] === 0xff) {
    const marker = bytes[pos + 1];
    if (marker === 0xda || marker === 0xd9) break;
    const segLen = (bytes[pos + 2] << 8) | bytes[pos + 3];
    if (segLen < 2 || pos + 2 + segLen > bytes.byteLength) break;
    const isExif =
      marker === 0xe1 &&
      bytes[pos + 4] === 0x45 &&
      bytes[pos + 5] === 0x78 &&
      bytes[pos + 6] === 0x69 &&
      bytes[pos + 7] === 0x66 &&
      bytes[pos + 8] === 0 &&
      bytes[pos + 9] === 0;
    if (isExif) {
      const tiff = bytes.slice(pos + 10, pos + 2 + segLen);
      if (tiff.byteLength < 8) break;
      const view = new DataView(tiff.buffer, tiff.byteOffset, tiff.byteLength);
      const order = String.fromCharCode(tiff[0], tiff[1]);
      if (order !== "II" && order !== "MM") break;
      const little = order === "II";
      if (view.getUint16(2, little) !== 42) break;
      const ifd0 = parseIfd(view, little, view.getUint32(4, little));
      if (typeof ifd0.get(0x0112) === "number") out.orientation = ifd0.get(0x0112) as number;
      if (typeof ifd0.get(0x0132) === "string") out.datetime = ifd0.get(0x0132) as string;
      const exifOff = ifd0.get(0x8769);
      if (typeof exifOff === "number") {
        const exifIfd = parseIfd(view, little, exifOff);
        if (typeof exifIfd.get(0x9003) === "string") out.datetime = exifIfd.get(0x9003) as string;
      }
      const gpsOff = ifd0.get(0x8825);
      if (typeof gpsOff === "number") {
        const gps = parseIfd(view, little, gpsOff);
        const dms = (v: unknown) =>
          Array.isArray(v) && v.length === 3 ? (v[0] as number) + (v[1] as number) / 60 + (v[2] as number) / 3600 : null;
        const lat = dms(gps.get(0x0002));
        const lon = dms(gps.get(0x0004));
        if (lat !== null) out.gps_latitude = Math.round((String(gps.get(0x0001)).toUpperCase() === "S" ? -lat : lat) * 1e6) / 1e6;
        if (lon !== null) out.gps_longitude = Math.round((String(gps.get(0x0003)).toUpperCase() === "W" ? -lon : lon) * 1e6) / 1e6;
      }
      break;
    }
    pos += 2 + segLen;
  }
  return out;
}

/** Remove APP1 (EXIF/XMP), APP2, APP13 (IPTC) and COM segments from a JPEG. */
export function stripJpegExifSegments(bytes: Uint8Array): Uint8Array {
  if (bytes.byteLength < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return bytes;
  const chunks: Uint8Array[] = [bytes.slice(0, 2)];
  let pos = 2;
  const stripMarkers = new Set([0xe1, 0xe2, 0xed, 0xfe]);
  while (pos + 2 <= bytes.byteLength) {
    if (bytes[pos] !== 0xff) {
      chunks.push(bytes.slice(pos));
      break;
    }
    const marker = bytes[pos + 1];
    if (marker === 0xda) {
      chunks.push(bytes.slice(pos));
      break;
    }
    if (marker === 0xd8 || marker === 0xd9 || marker === 0x00 || (marker >= 0xd0 && marker <= 0xd7)) {
      chunks.push(bytes.slice(pos, pos + 2));
      pos += 2;
      continue;
    }
    if (pos + 4 > bytes.byteLength) break;
    const segLen = (bytes[pos + 2] << 8) | bytes[pos + 3];
    const segEnd = pos + 2 + segLen;
    if (segLen < 2 || segEnd > bytes.byteLength) break;
    if (!stripMarkers.has(marker)) chunks.push(bytes.slice(pos, segEnd));
    pos = segEnd;
  }
  const total = chunks.reduce((acc, c) => acc + c.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

/** Great-circle distance in metres (reviewer EXIF-vs-submitted comparison). */
export function haversineMeters(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const r = 6371008.8;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * r * Math.asin(Math.min(1, Math.sqrt(a)));
}
