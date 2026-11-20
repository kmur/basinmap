/**
 * Deterministic rebuild of /overlays/approved.geojson (AGENTS.md invariant 3):
 * always a full `SELECT ... ORDER BY occurrence_id ASC`, never read-modify-write.
 *
 * Records are removed from the provisional overlay only once the *live map release*
 * contains them (its `layers.snapshot_own_occurrence_ids` list). Being indexed by GBIF
 * is not enough: the map snapshot is rebuilt monthly, so dropping indexed records
 * earlier would make them disappear from the map until the next release.
 */

async function liveSnapshotIds(publicBucket: R2Bucket): Promise<Set<string>> {
  const ids = new Set<string>();
  try {
    const manifestObj = await publicBucket.get("releases/manifest.json");
    if (!manifestObj) return ids;
    const manifest = JSON.parse(await manifestObj.text()) as { layers?: { snapshot_own_occurrence_ids?: string } };
    const path = manifest.layers?.snapshot_own_occurrence_ids;
    if (!path) return ids;
    const listObj = await publicBucket.get(path.replace(/^\/+/, ""));
    if (!listObj) return ids;
    const list = JSON.parse(await listObj.text());
    if (Array.isArray(list)) for (const id of list) if (typeof id === "string") ids.add(id);
  } catch (_err) {
    // A malformed manifest must never block the overlay; frontend dedup still applies.
  }
  return ids;
}

export async function rebuildApprovedOverlay(db: D1Database, publicBucket: R2Bucket): Promise<number> {
  const inSnapshot = await liveSnapshotIds(publicBucket);
  const { results } = await db
    .prepare(`SELECT * FROM observations WHERE review_status = 'approved' ORDER BY occurrence_id ASC`)
    .all<Record<string, unknown>>();

  const features: Array<Record<string, unknown>> = [];
  for (const row of results) {
    if (inSnapshot.has(String(row.occurrence_id))) continue;
    const mediaRes = await db
      .prepare(
        `SELECT public_r2_key FROM media_intake
         WHERE occurrence_id = ? AND derivative_status = 'public_sanitized'
         ORDER BY media_id ASC`
      )
      .bind(row.occurrence_id)
      .all<{ public_r2_key: string }>();

    features.push({
      type: "Feature",
      id: row.occurrence_id,
      properties: {
        occurrenceID: row.occurrence_id,
        scientificName: row.scientific_name,
        taxonKey: row.taxon_key,
        eventDate: row.event_date,
        year: row.year,
        basisOfRecord: row.basis_of_record,
        hybas_id: row.hybas_id,
        coordinateUncertaintyInMeters: row.public_coordinate_uncertainty_m,
        informationWithheld: row.information_withheld,
        isSensitiveSpecies: Boolean(row.is_sensitive_species),
        license: row.license,
        rightsHolder: row.rights_holder,
        recordedByID: row.recorded_by_id,
        identifiedByID: row.identified_by_id,
        reviewStatus: row.review_status,
        publicationStatus: row.publication_status,
        gbifOccurrenceKey: row.gbif_occurrence_key ?? null,
        provisional: true,
        mediaUrls: mediaRes.results.map((m) => `/${m.public_r2_key}`),
      },
      geometry: {
        type: "Point",
        coordinates: [row.public_longitude, row.public_latitude],
      },
    });
  }

  const fc = {
    type: "FeatureCollection",
    name: "approved_local_observations",
    generated_at: new Date().toISOString(),
    features,
  };

  await publicBucket.put("overlays/approved.geojson", JSON.stringify(fc, null, 2) + "\n", {
    httpMetadata: {
      contentType: "application/json; charset=utf-8",
      cacheControl: "public, max-age=60, must-revalidate",
    },
  });
  return features.length;
}
