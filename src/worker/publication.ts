/**
 * Automated GBIF publication pipeline (hosting plan Section 5.2), run by the Worker cron
 * trigger and by POST /api/admin/publication/run.
 *
 *   1. Poll pending GBIF Data Validator jobs (GET /v1/validation/{key}).
 *   2. Reconcile: records GBIF has indexed from our registered dataset
 *      -> publication_status = 'indexed_by_gbif' + gbif_occurrence_key.
 *   3. Export the Darwin Core Archive (eml.xml, meta.xml, occurrence.txt, multimedia.txt)
 *      to /exports/dwca-latest.zip (+ immutable /exports/archive/<export_id>.zip) when the
 *      record content changed since the last export.
 *   4. For a new export: submit it to the GBIF Data Validator and ask the registry to
 *      re-crawl the dataset endpoint.
 *   5. Publish /exports/status.json (no private data).
 *
 * The archive layout mirrors scripts/export_dwca.py; tests validate Worker-built archives
 * with export_dwca.validate_dwca_zip.
 */

import { rebuildApprovedOverlay } from "./overlay.ts";

export interface PublicationEnv {
  DB?: D1Database;
  PUBLIC_BUCKET?: R2Bucket;
  ENVIRONMENT?: string;
  DATASET_LICENSE?: string;
  PUBLICATION_BASE_URL?: string;
  GBIF_API_BASE?: string;
  GBIF_VALIDATOR_URL?: string;
  GBIF_DATASET_KEY?: string;
  GBIF_USER?: string;
  GBIF_PWD?: string;
  GBIF_VALIDATE?: string;
  DATASET_CONTACT_GIVEN_NAME?: string;
  DATASET_CONTACT_SURNAME?: string;
  DATASET_CONTACT_EMAIL?: string;
}

export interface DatasetContact {
  givenName: string;
  surName: string;
  email: string;
}

export const DEFAULT_CONTACT: DatasetContact = { givenName: "", surName: "", email: "stewardship@basinmap.org" };

/** Named EML contact from Worker vars/secrets; GBIF warns (RESOURCE_CONTACTS_MISSING_OR_INCOMPLETE) without a person. */
export function datasetContact(env: PublicationEnv): DatasetContact {
  return {
    givenName: env.DATASET_CONTACT_GIVEN_NAME || "",
    surName: env.DATASET_CONTACT_SURNAME || "",
    email: env.DATASET_CONTACT_EMAIL || DEFAULT_CONTACT.email,
  };
}

export const LICENSE_URI_MAP: Record<string, string> = {
  "CC-BY 4.0": "http://creativecommons.org/licenses/by/4.0/legalcode",
  "CC0 1.0": "http://creativecommons.org/publicdomain/zero/1.0/legalcode",
};

const OCCURRENCE_FIELDS: Array<[string, string]> = [
  ["occurrenceID", "http://rs.tdwg.org/dwc/terms/occurrenceID"],
  ["basisOfRecord", "http://rs.tdwg.org/dwc/terms/basisOfRecord"],
  ["scientificName", "http://rs.tdwg.org/dwc/terms/scientificName"],
  ["taxonKey", "http://rs.gbif.org/terms/1.0/taxonKey"],
  ["kingdom", "http://rs.tdwg.org/dwc/terms/kingdom"],
  ["family", "http://rs.tdwg.org/dwc/terms/family"],
  ["eventDate", "http://rs.tdwg.org/dwc/terms/eventDate"],
  ["decimalLatitude", "http://rs.tdwg.org/dwc/terms/decimalLatitude"],
  ["decimalLongitude", "http://rs.tdwg.org/dwc/terms/decimalLongitude"],
  ["geodeticDatum", "http://rs.tdwg.org/dwc/terms/geodeticDatum"],
  ["coordinateUncertaintyInMeters", "http://rs.tdwg.org/dwc/terms/coordinateUncertaintyInMeters"],
  ["informationWithheld", "http://rs.tdwg.org/dwc/terms/informationWithheld"],
  ["locality", "http://rs.tdwg.org/dwc/terms/locality"],
  ["recordedBy", "http://rs.tdwg.org/dwc/terms/recordedBy"],
  ["recordedByID", "http://rs.tdwg.org/dwc/terms/recordedByID"],
  ["identifiedBy", "http://rs.tdwg.org/dwc/terms/identifiedBy"],
  ["identifiedByID", "http://rs.tdwg.org/dwc/terms/identifiedByID"],
  ["license", "http://purl.org/dc/terms/license"],
  ["rightsHolder", "http://purl.org/dc/terms/rightsHolder"],
  ["modified", "http://purl.org/dc/terms/modified"],
];

const MULTIMEDIA_FIELDS: Array<[string, string | null]> = [
  ["coreid", null],
  ["identifier", "http://purl.org/dc/terms/identifier"],
  ["type", "http://purl.org/dc/terms/type"],
  ["format", "http://purl.org/dc/terms/format"],
  ["created", "http://purl.org/dc/terms/created"],
  ["creator", "http://purl.org/dc/terms/creator"],
  ["license", "http://purl.org/dc/terms/license"],
  ["rightsHolder", "http://purl.org/dc/terms/rightsHolder"],
];

const VALIDATOR_PENDING = new Set(["submitted", "queued", "running", "downloading"]);
const MAX_RECONCILE_PAGES = 40; // 40 x 300 = 12,000 records per run; resumes next run.

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function tsvCell(val: unknown): string {
  if (val === null || val === undefined) return "";
  return String(val).replace(/[\t\r\n]/g, " ").trim();
}

function xmlEscape(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function sha256Hex(data: Uint8Array | string): Promise<string> {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
  const digest = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function gbifAuth(env: PublicationEnv): string | null {
  if (!env.GBIF_USER || !env.GBIF_PWD) return null;
  return `Basic ${btoa(`${env.GBIF_USER}:${env.GBIF_PWD}`)}`;
}

function gbifApiBase(env: PublicationEnv): string {
  return (env.GBIF_API_BASE || "https://api.gbif.org/v1").replace(/\/+$/, "");
}

// ---------------------------------------------------------------------------
// Zip writer (deflate via CompressionStream("deflate-raw"); CRC-32 per entry)
// ---------------------------------------------------------------------------

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (let i = 0; i < data.length; i++) crc = CRC_TABLE[(crc ^ data[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

async function deflateRaw(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new CompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

export async function buildZip(files: Array<{ name: string; data: Uint8Array }>, when: Date): Promise<Uint8Array> {
  const dosTime = (when.getUTCHours() << 11) | (when.getUTCMinutes() << 5) | Math.floor(when.getUTCSeconds() / 2);
  const dosDate = ((when.getUTCFullYear() - 1980) << 9) | ((when.getUTCMonth() + 1) << 5) | when.getUTCDate();
  const local: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const f of files) {
    const nameBytes = new TextEncoder().encode(f.name);
    const comp = await deflateRaw(f.data);
    const crc = crc32(f.data);
    const lh = new DataView(new ArrayBuffer(30));
    lh.setUint32(0, 0x04034b50, true);
    lh.setUint16(4, 20, true);
    lh.setUint16(6, 0x0800, true); // UTF-8 names
    lh.setUint16(8, 8, true); // deflate
    lh.setUint16(10, dosTime, true);
    lh.setUint16(12, dosDate, true);
    lh.setUint32(14, crc, true);
    lh.setUint32(18, comp.length, true);
    lh.setUint32(22, f.data.length, true);
    lh.setUint16(26, nameBytes.length, true);
    lh.setUint16(28, 0, true);
    local.push(new Uint8Array(lh.buffer), nameBytes, comp);

    const ch = new DataView(new ArrayBuffer(46));
    ch.setUint32(0, 0x02014b50, true);
    ch.setUint16(4, 20, true);
    ch.setUint16(6, 20, true);
    ch.setUint16(8, 0x0800, true);
    ch.setUint16(10, 8, true);
    ch.setUint16(12, dosTime, true);
    ch.setUint16(14, dosDate, true);
    ch.setUint32(16, crc, true);
    ch.setUint32(20, comp.length, true);
    ch.setUint32(24, f.data.length, true);
    ch.setUint16(28, nameBytes.length, true);
    ch.setUint32(42, offset, true);
    central.push(new Uint8Array(ch.buffer), nameBytes);
    offset += 30 + nameBytes.length + comp.length;
  }
  const centralSize = central.reduce((acc, c) => acc + c.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);
  const parts = [...local, ...central, new Uint8Array(end.buffer)];
  const out = new Uint8Array(parts.reduce((acc, p) => acc + p.length, 0));
  let pos = 0;
  for (const p of parts) {
    out.set(p, pos);
    pos += p.length;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Darwin Core Archive content (mirrors scripts/export_dwca.py)
// ---------------------------------------------------------------------------

export function buildMetaXml(): string {
  const occ: string[] = [];
  OCCURRENCE_FIELDS.forEach(([, term], idx) => {
    if (idx === 0) occ.push('    <id index="0" />');
    occ.push(`    <field index="${idx}" term="${term}" />`);
  });
  const mm: string[] = ['    <coreid index="0" />'];
  MULTIMEDIA_FIELDS.forEach(([, term], idx) => {
    if (term) mm.push(`    <field index="${idx}" term="${term}" />`);
  });
  return (
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<archive xmlns="http://rs.tdwg.org/dwc/text/" metadata="eml.xml">\n' +
    '  <core encoding="UTF-8" fieldsTerminatedBy="\\t" linesTerminatedBy="\\n" ' +
    'fieldsEnclosedBy="" ignoreHeaderLines="1" rowType="http://rs.tdwg.org/dwc/terms/Occurrence">\n' +
    "    <files>\n      <location>occurrence.txt</location>\n    </files>\n" +
    occ.join("\n") +
    "\n  </core>\n" +
    '  <extension encoding="UTF-8" fieldsTerminatedBy="\\t" linesTerminatedBy="\\n" ' +
    'fieldsEnclosedBy="" ignoreHeaderLines="1" rowType="http://rs.gbif.org/terms/1.0/Multimedia">\n' +
    "    <files>\n      <location>multimedia.txt</location>\n    </files>\n" +
    mm.join("\n") +
    "\n  </extension>\n</archive>\n"
  );
}

export function buildEmlXml(
  exportId: string,
  datasetLicense: string,
  bbox: number[],
  minDate: string,
  maxDate: string,
  pubDate: string,
  contact: DatasetContact = DEFAULT_CONTACT
): string {
  const licenseUri = LICENSE_URI_MAP[datasetLicense];
  const [west, south, east, north] = bbox;
  const e = xmlEscape;
  const person =
    contact.givenName || contact.surName
      ? `      <individualName><givenName>${e(contact.givenName)}</givenName><surName>${e(contact.surName)}</surName></individualName>\n`
      : "";
  return `<?xml version="1.0" encoding="UTF-8"?>
<eml:eml xmlns:eml="eml://ecoinformatics.org/eml-2.1.1"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="eml://ecoinformatics.org/eml-2.1.1 http://rs.gbif.org/schema/eml-gbif-profile/1.2/eml.xsd"
         packageId="${e(exportId)}"
         system="https://basinmap.org"
         scope="system"
         xml:lang="eng">
  <dataset>
    <alternateIdentifier>${e(exportId)}</alternateIdentifier>
    <title xml:lang="eng">basinmap.org Community Hydrological Basin Biodiversity Observations</title>
    <creator>
${person}      <organizationName>basinmap.org Open Web-GIS Project</organizationName>
      <positionName>Data Steward</positionName>
      <electronicMailAddress>${e(contact.email)}</electronicMailAddress>
      <onlineUrl>https://basinmap.org</onlineUrl>
    </creator>
    <metadataProvider>
${person}      <organizationName>basinmap.org Open Web-GIS Project</organizationName>
      <positionName>Data Steward</positionName>
      <electronicMailAddress>${e(contact.email)}</electronicMailAddress>
      <onlineUrl>https://basinmap.org</onlineUrl>
    </metadataProvider>
    <pubDate>${e(pubDate)}</pubDate>
    <language>eng</language>
    <abstract>
      <para>Reviewed biodiversity observations contributed via basinmap.org with hydrological basin context, preserved verbatim metadata, and coordinate generalization for sensitive taxa.</para>
    </abstract>
    <intellectualRights>
      <para>This work is licensed under <ulink url="${e(licenseUri)}"><citetitle>${e(datasetLicense)}</citetitle></ulink>.</para>
    </intellectualRights>
    <coverage>
      <geographicCoverage>
        <geographicDescription>Hydrological basins mapped by basinmap.org</geographicDescription>
        <boundingCoordinates>
          <westBoundingCoordinate>${west.toFixed(4)}</westBoundingCoordinate>
          <eastBoundingCoordinate>${east.toFixed(4)}</eastBoundingCoordinate>
          <northBoundingCoordinate>${north.toFixed(4)}</northBoundingCoordinate>
          <southBoundingCoordinate>${south.toFixed(4)}</southBoundingCoordinate>
        </boundingCoordinates>
      </geographicCoverage>
      <temporalCoverage>
        <rangeOfDates>
          <beginDate><calendarDate>${e(minDate)}</calendarDate></beginDate>
          <endDate><calendarDate>${e(maxDate)}</calendarDate></endDate>
        </rangeOfDates>
      </temporalCoverage>
    </coverage>
    <contact>
${person}      <organizationName>basinmap.org Data Stewardship</organizationName>
      <positionName>Data Steward</positionName>
      <electronicMailAddress>${e(contact.email)}</electronicMailAddress>
      <onlineUrl>https://basinmap.org</onlineUrl>
    </contact>
  </dataset>
</eml:eml>
`;
}

/** Licences that may be published inside a dataset under `datasetLicense`. */
export function compatibleLicenses(datasetLicense: string): string[] {
  // CC0 waives all rights, so CC0 records may be redistributed under CC-BY; not vice versa.
  return datasetLicense === "CC-BY 4.0" ? ["CC-BY 4.0", "CC0 1.0"] : [datasetLicense];
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

export interface ExportResult {
  changed: boolean;
  export_id: string | null;
  record_count: number;
  media_count: number;
  excluded_license_count: number;
  withdrawn_count: number;
  newly_exported: number;
  sha256?: string;
  zip?: Uint8Array;
}

export async function exportDwca(env: PublicationEnv, force = false): Promise<ExportResult> {
  const db = env.DB as D1Database;
  const bucket = env.PUBLIC_BUCKET as R2Bucket;
  const datasetLicense = env.DATASET_LICENSE || "CC-BY 4.0";
  if (!LICENSE_URI_MAP[datasetLicense]) throw new Error(`Unsupported DATASET_LICENSE ${datasetLicense}`);
  const licenseUri = LICENSE_URI_MAP[datasetLicense];
  const allowed = compatibleLicenses(datasetLicense);
  const base = (env.PUBLICATION_BASE_URL || "https://data.basinmap.org").replace(/\/+$/, "");

  const rows = (
    await db
      .prepare(
        `SELECT * FROM observations WHERE review_status = 'approved' AND license IN (${allowed.map(() => "?").join(",")})
         ORDER BY occurrence_id ASC`
      )
      .bind(...allowed)
      .all<Record<string, unknown>>()
  ).results;
  const excluded = await db
    .prepare(
      `SELECT COUNT(*) AS n FROM observations WHERE review_status = 'approved' AND license NOT IN (${allowed.map(() => "?").join(",")})`
    )
    .bind(...allowed)
    .first<{ n: number }>();

  const occLines = [OCCURRENCE_FIELDS.map(([c]) => c).join("\t")];
  const mmLines = [MULTIMEDIA_FIELDS.map(([c]) => c).join("\t")];
  const lats: number[] = [];
  const lons: number[] = [];
  const dates: string[] = [];
  let mediaCount = 0;
  for (const row of rows) {
    lats.push(Number(row.public_latitude));
    lons.push(Number(row.public_longitude));
    dates.push(String(row.event_date).slice(0, 10));
    occLines.push(
      [
        row.occurrence_id,
        row.basis_of_record,
        row.scientific_name,
        row.taxon_key,
        row.kingdom,
        row.family,
        row.event_date,
        row.public_latitude,
        row.public_longitude,
        row.geodetic_datum,
        row.public_coordinate_uncertainty_m,
        row.information_withheld,
        row.locality,
        row.recorded_by,
        row.recorded_by_id,
        row.identified_by,
        row.identified_by_id,
        licenseUri,
        row.rights_holder,
        row.updated_at,
      ]
        .map(tsvCell)
        .join("\t")
    );
    const media = await db
      .prepare(
        `SELECT * FROM media_intake WHERE occurrence_id = ? AND derivative_status = 'public_sanitized' ORDER BY media_id ASC`
      )
      .bind(row.occurrence_id)
      .all<Record<string, unknown>>();
    for (const m of media.results) {
      if (!m.public_r2_key) continue;
      mediaCount += 1;
      const key = String(m.public_r2_key);
      mmLines.push(
        [
          row.occurrence_id,
          `${base}/${key.replace(/^\/+/, "")}`,
          "StillImage",
          key.endsWith(".jpg") ? "image/jpeg" : m.detected_mime_type,
          m.created_at,
          row.recorded_by,
          LICENSE_URI_MAP[String(m.license)] || licenseUri,
          m.rights_holder,
        ]
          .map(tsvCell)
          .join("\t")
      );
    }
  }
  const occTxt = occLines.join("\n") + "\n";
  const mmTxt = mmLines.join("\n") + "\n";
  const contentSha = await sha256Hex(`${occTxt}\u0000${mmTxt}`);

  const last = await db
    .prepare("SELECT content_sha256 FROM publication_exports ORDER BY created_at DESC LIMIT 1")
    .first<{ content_sha256: string | null }>();

  // Records exported earlier that are no longer approved are withdrawn: the next archive omits
  // them, so GBIF deletes them on re-crawl. Their identifiers stay stable for re-approval.
  const now = nowIso();
  const withdrawn = await db
    .prepare(
      `SELECT occurrence_id, revision_number, COALESCE(reviewer_user_id, submitter_user_id) AS actor
       FROM observations WHERE review_status != 'approved' AND publication_status != 'not_exported'`
    )
    .all<{ occurrence_id: string; revision_number: number; actor: string }>();

  if (!force && last && last.content_sha256 === contentSha && withdrawn.results.length === 0) {
    return {
      changed: false,
      export_id: null,
      record_count: rows.length,
      media_count: mediaCount,
      excluded_license_count: Number(excluded?.n || 0),
      withdrawn_count: 0,
      newly_exported: 0,
    };
  }

  const stamp = now.replace(/[-:]/g, "").replace(/\.\d+/, "");
  const exportId = `basinmap-dwca-${stamp}-${crypto.randomUUID().slice(0, 8)}`;
  const today = now.slice(0, 10);
  const bbox = lons.length
    ? [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)]
    : [-123.8, 43.5, -121.5, 46.2];
  const sorted = [...dates].sort();
  const eml = buildEmlXml(exportId, datasetLicense, bbox, sorted[0] || today, sorted[sorted.length - 1] || today, today, datasetContact(env));
  const enc = new TextEncoder();
  const zip = await buildZip(
    [
      { name: "eml.xml", data: enc.encode(eml) },
      { name: "meta.xml", data: enc.encode(buildMetaXml()) },
      { name: "occurrence.txt", data: enc.encode(occTxt) },
      { name: "multimedia.txt", data: enc.encode(mmTxt) },
    ],
    new Date()
  );
  const zipSha = await sha256Hex(zip);
  const snapshotKey = `exports/archive/${exportId}.zip`;

  // Immutable snapshot first, then the stable URL GBIF crawls, then metadata.
  await bucket.put(snapshotKey, zip, {
    httpMetadata: { contentType: "application/zip", cacheControl: "public, max-age=31536000, immutable" },
  });
  await bucket.put("exports/dwca-latest.zip", zip, {
    httpMetadata: { contentType: "application/zip", cacheControl: "public, max-age=300, must-revalidate" },
  });
  await bucket.put("exports/eml.xml", eml, {
    httpMetadata: { contentType: "application/xml; charset=utf-8", cacheControl: "public, max-age=300, must-revalidate" },
  });

  // Publication status only moves forward: not_exported -> exported. Records GBIF has already
  // indexed (or a provider has published) keep their status.
  const ids = rows.map((r) => String(r.occurrence_id));
  const fresh = rows.filter((r) => r.publication_status === "not_exported");
  const stmts: D1PreparedStatement[] = [];
  for (const id of ids) {
    stmts.push(db.prepare("UPDATE observations SET exported_dwca_version = ? WHERE occurrence_id = ?").bind(exportId, id));
  }
  for (const r of fresh) {
    const rev = Number(r.revision_number) + 1;
    stmts.push(
      db
        // updated_at is Darwin Core `modified` (record content); publication status changes must not
        // touch it, or every export would change the archive and trigger another GBIF crawl.
        .prepare("UPDATE observations SET publication_status = 'exported', revision_number = ? WHERE occurrence_id = ?")
        .bind(rev, r.occurrence_id)
    );
    stmts.push(
      db
        .prepare(
          `INSERT INTO observation_revisions (occurrence_id, revision_number, actor_user_id, change_type, snapshot_json, notes, created_at)
           VALUES (?, ?, ?, 'exported', ?, ?, ?)`
        )
        .bind(
          r.occurrence_id,
          rev,
          r.reviewer_user_id || r.submitter_user_id,
          JSON.stringify({ occurrence_id: r.occurrence_id, exported_dwca_version: exportId }),
          `Exported in DwC-A ${exportId}`,
          now
        )
    );
  }
  for (const w of withdrawn.results) {
    const rev = Number(w.revision_number) + 1;
    stmts.push(
      db
        .prepare("UPDATE observations SET publication_status = 'not_exported', revision_number = ? WHERE occurrence_id = ?")
        .bind(rev, w.occurrence_id)
    );
    stmts.push(
      db
        .prepare(
          `INSERT INTO observation_revisions (occurrence_id, revision_number, actor_user_id, change_type, snapshot_json, notes, created_at)
           VALUES (?, ?, ?, 'updated', ?, ?, ?)` // change_type CHECK (0001) has no 'withdrawn'; the note records it
        )
        .bind(w.occurrence_id, rev, w.actor, JSON.stringify({ occurrence_id: w.occurrence_id, withdrawn_from: exportId }), `Withdrawn: omitted from DwC-A ${exportId} (no longer approved)`, now)
    );
  }
  stmts.push(
    db
      .prepare(
        `INSERT INTO publication_exports
          (export_id, archive_r2_key, dataset_license, record_count, media_count, sha256, validator_status, created_at,
           content_sha256, archive_snapshot_r2_key, excluded_license_count)
         VALUES (?, 'exports/dwca-latest.zip', ?, ?, ?, ?, 'not_submitted', ?, ?, ?, ?)`
      )
      .bind(exportId, datasetLicense, rows.length, mediaCount, zipSha, now, contentSha, snapshotKey, Number(excluded?.n || 0))
  );
  for (let i = 0; i < stmts.length; i += 50) await db.batch(stmts.slice(i, i + 50));

  return {
    changed: true,
    export_id: exportId,
    record_count: rows.length,
    media_count: mediaCount,
    excluded_license_count: Number(excluded?.n || 0),
    withdrawn_count: withdrawn.results.length,
    newly_exported: fresh.length,
    sha256: zipSha,
    zip,
  };
}

// ---------------------------------------------------------------------------
// GBIF Data Validator, registry crawl, reconciliation
// ---------------------------------------------------------------------------

export async function submitValidation(env: PublicationEnv, exportId: string, zip: Uint8Array): Promise<string> {
  const auth = gbifAuth(env);
  if (!auth || env.GBIF_VALIDATE === "false") return "not_submitted";
  const url = env.GBIF_VALIDATOR_URL || "https://api.gbif.org/v1/validation";
  const form = new FormData();
  form.append("file", new Blob([zip as BlobPart], { type: "application/zip" }), `${exportId}.zip`);
  form.append(
    "validationRequest",
    new Blob([JSON.stringify({ sourceId: `basinmap.org/${exportId}` })], { type: "application/json" })
  );
  const resp = await fetch(url, { method: "POST", headers: { authorization: auth, "user-agent": "basinmap.org" }, body: form });
  const body = (await resp.json().catch(() => ({}))) as { key?: string; status?: string };
  const status = resp.ok && body.key ? String(body.status || "submitted").toLowerCase() : `error_${resp.status}`;
  await (env.DB as D1Database)
    .prepare("UPDATE publication_exports SET validator_job_id = ?, validator_status = ?, validator_checked_at = ? WHERE export_id = ?")
    .bind(body.key || null, status, nowIso(), exportId)
    .run();
  return status;
}

export async function pollValidations(env: PublicationEnv): Promise<Array<{ export_id: string; status: string }>> {
  const auth = gbifAuth(env);
  if (!auth) return [];
  const db = env.DB as D1Database;
  const url = (env.GBIF_VALIDATOR_URL || "https://api.gbif.org/v1/validation").replace(/\/+$/, "");
  const pending = await db
    .prepare(
      `SELECT export_id, validator_job_id FROM publication_exports
       WHERE validator_job_id IS NOT NULL AND validator_status IN ('submitted','queued','running','downloading')`
    )
    .all<{ export_id: string; validator_job_id: string }>();
  const out: Array<{ export_id: string; status: string }> = [];
  for (const p of pending.results) {
    const resp = await fetch(`${url}/${encodeURIComponent(p.validator_job_id)}`, { headers: { authorization: auth } });
    if (!resp.ok) continue;
    const body = (await resp.json().catch(() => ({}))) as Record<string, unknown>;
    const status = String(body.status || "unknown").toLowerCase();
    const summary = VALIDATOR_PENDING.has(status) ? null : JSON.stringify(body).slice(0, 20000);
    await db
      .prepare("UPDATE publication_exports SET validator_status = ?, validator_summary = COALESCE(?, validator_summary), validator_checked_at = ? WHERE export_id = ?")
      .bind(status, summary, nowIso(), p.export_id)
      .run();
    out.push({ export_id: p.export_id, status });
  }
  return out;
}

export async function requestCrawl(env: PublicationEnv, exportId: string): Promise<string> {
  const auth = gbifAuth(env);
  if (!env.GBIF_DATASET_KEY) return "no_dataset_key";
  if (!auth) return "no_credentials";
  const resp = await fetch(`${gbifApiBase(env)}/dataset/${encodeURIComponent(env.GBIF_DATASET_KEY)}/crawl`, {
    method: "POST",
    headers: { authorization: auth, "user-agent": "basinmap.org" },
  });
  const status = resp.ok ? "requested" : resp.status === 401 || resp.status === 403 ? "not_authorized" : `error_${resp.status}`;
  await (env.DB as D1Database).prepare("UPDATE publication_exports SET crawl_status = ? WHERE export_id = ?").bind(status, exportId).run();
  return status;
}

export async function reconcileWithGbif(env: PublicationEnv): Promise<{ checked: number; newly_indexed: number; complete: boolean }> {
  if (!env.GBIF_DATASET_KEY) return { checked: 0, newly_indexed: 0, complete: true };
  const db = env.DB as D1Database;
  const base = gbifApiBase(env);
  let offset = 0;
  let checked = 0;
  let newly = 0;
  let complete = false;
  const now = nowIso();
  for (let page = 0; page < MAX_RECONCILE_PAGES; page++) {
    const resp = await fetch(
      `${base}/occurrence/search?datasetKey=${encodeURIComponent(env.GBIF_DATASET_KEY)}&limit=300&offset=${offset}`,
      { headers: { "user-agent": "basinmap.org" } }
    );
    if (!resp.ok) break;
    const body = (await resp.json()) as { results?: Array<{ key?: number; occurrenceID?: string }>; endOfRecords?: boolean };
    const results = body.results || [];
    const stmts: D1PreparedStatement[] = [];
    for (const r of results) {
      if (!r.occurrenceID || !r.key) continue;
      checked += 1;
      stmts.push(
        db
          .prepare(
            `UPDATE observations SET publication_status = 'indexed_by_gbif', gbif_occurrence_key = ?, gbif_indexed_at = ?
             WHERE occurrence_id = ? AND review_status = 'approved'
               AND (publication_status != 'indexed_by_gbif' OR gbif_occurrence_key IS NULL OR gbif_occurrence_key != ?)`
          )
          .bind(r.key, now, r.occurrenceID, r.key)
      );
    }
    if (stmts.length) {
      const before = await db.prepare("SELECT COUNT(*) AS n FROM observations WHERE publication_status = 'indexed_by_gbif'").first<{ n: number }>();
      for (let i = 0; i < stmts.length; i += 50) await db.batch(stmts.slice(i, i + 50));
      const after = await db.prepare("SELECT COUNT(*) AS n FROM observations WHERE publication_status = 'indexed_by_gbif'").first<{ n: number }>();
      newly += Number(after?.n || 0) - Number(before?.n || 0);
    }
    offset += results.length;
    if (body.endOfRecords || results.length === 0) {
      complete = true;
      break;
    }
  }
  return { checked, newly_indexed: newly, complete };
}

// ---------------------------------------------------------------------------
// Status file + orchestration
// ---------------------------------------------------------------------------

export async function writeStatus(env: PublicationEnv): Promise<Record<string, unknown>> {
  const db = env.DB as D1Database;
  const base = (env.PUBLICATION_BASE_URL || "https://data.basinmap.org").replace(/\/+$/, "");
  const latest = await db
    .prepare(
      `SELECT export_id, created_at, record_count, media_count, sha256, archive_snapshot_r2_key, validator_status,
              validator_checked_at, validator_summary, crawl_status, excluded_license_count
       FROM publication_exports ORDER BY created_at DESC LIMIT 1`
    )
    .first<Record<string, unknown>>();
  const counts = await db
    .prepare(
      `SELECT publication_status AS s, COUNT(*) AS n FROM observations WHERE review_status = 'approved' GROUP BY publication_status`
    )
    .all<{ s: string; n: number }>();
  let indexeable: unknown = null;
  if (latest?.validator_summary) {
    try {
      const v = JSON.parse(String(latest.validator_summary)) as Record<string, any>;
      // GBIF reports the verdict as metrics.indexeable; accept a top-level value too.
      indexeable = v?.metrics?.indexeable ?? v?.indexeable ?? null;
    } catch (_err) {
      indexeable = null;
    }
  }
  const status = {
    generated_at: nowIso(),
    dataset_license: env.DATASET_LICENSE || "CC-BY 4.0",
    archive_url: `${base}/exports/dwca-latest.zip`,
    latest_export: latest
      ? {
          export_id: latest.export_id,
          created_at: latest.created_at,
          record_count: latest.record_count,
          media_count: latest.media_count,
          sha256: latest.sha256,
          snapshot_url: latest.archive_snapshot_r2_key ? `${base}/${latest.archive_snapshot_r2_key}` : null,
          excluded_license_count: latest.excluded_license_count ?? 0,
          gbif_validator: { status: latest.validator_status, checked_at: latest.validator_checked_at, indexeable },
          gbif_crawl: latest.crawl_status ?? null,
        }
      : null,
    gbif: { api_base: gbifApiBase(env), dataset_key: env.GBIF_DATASET_KEY || null },
    approved_records_by_publication_status: Object.fromEntries(counts.results.map((c) => [c.s, Number(c.n)])),
    note: "Approved records appear on GBIF only after the registered dataset is crawled; see publication_status.",
  };
  await (env.PUBLIC_BUCKET as R2Bucket).put("exports/status.json", JSON.stringify(status, null, 2) + "\n", {
    httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "public, max-age=60, must-revalidate" },
  });
  return status;
}

export async function runPublication(env: PublicationEnv, trigger: string, force = false): Promise<Record<string, unknown>> {
  if (!env.DB || !env.PUBLIC_BUCKET) throw new Error("DB and PUBLIC_BUCKET bindings are required");
  const db = env.DB;
  const runId = crypto.randomUUID();
  const started = nowIso();
  await db.prepare("INSERT INTO publication_runs (run_id, trigger, started_at) VALUES (?, ?, ?)").bind(runId, trigger, started).run();
  const summary: Record<string, unknown> = { run_id: runId, trigger, started_at: started };
  try {
    summary.validations = await pollValidations(env);
    const rec = await reconcileWithGbif(env);
    summary.reconcile = rec;
    if (rec.newly_indexed > 0) summary.overlay_records = await rebuildApprovedOverlay(db, env.PUBLIC_BUCKET);
    const exp = await exportDwca(env, force);
    summary.export = { ...exp, zip: undefined };
    if (exp.changed && exp.export_id && exp.zip) {
      // GBIF rejects an archive with an empty occurrence core (OCCURRENCE_CORE_EMPTY), so only
      // validate and crawl once there is at least one record.
      if (Number(exp.record_count || 0) > 0) {
        summary.validator = await submitValidation(env, exp.export_id, exp.zip);
        summary.crawl = await requestCrawl(env, exp.export_id);
      } else {
        summary.validator = "skipped_empty";
        summary.crawl = "skipped_empty";
      }
    }
    summary.status = await writeStatus(env);
    await db
      .prepare("UPDATE publication_runs SET finished_at = ?, export_id = ?, summary_json = ? WHERE run_id = ?")
      .bind(nowIso(), exp.export_id, JSON.stringify(summary).slice(0, 20000), runId)
      .run();
    return summary;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await db
      .prepare("UPDATE publication_runs SET finished_at = ?, error = ?, summary_json = ? WHERE run_id = ?")
      .bind(nowIso(), message.slice(0, 2000), JSON.stringify(summary).slice(0, 20000), runId)
      .run();
    throw err;
  }
}
