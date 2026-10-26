"""
Ask an ArcGIS layer what it actually contains, before querying it.

WHY THIS EXISTS
---------------
On 2026-10-26 every one of 25 WVGIS parcel queries came back as HTTP 200
carrying `{"code": 400, "message": "Failed to execute query."}`. The same
queries worked in July. `enrich_columns.py`'s own comment records the cause
this service is known for:

    "WVGIS's WV_Parcels service errors out entirely (400 'Failed to execute
    query') ... on an unrecognized 'Shape__Area' outField -- verified live."

So an outField this layer does not have is a documented way to produce exactly
the error we are getting, and the field names in the registry
(`FullOwnerName`, `Acres_C`) were last confirmed against the live service in
July. A re-publish or a rename since then fits the evidence.

WHAT THIS DOES INSTEAD OF GUESSING
----------------------------------
It cannot be guessed from here: this container's egress proxy denies
services.wvgis.wvu.edu, so the field list cannot be checked while writing
this. Rather than pick a likely new name and hope, `describe()` fetches the
layer's own metadata once per run and `resolve_out_fields()` matches the
configured names against what the layer reports.

Three outcomes, and each one tells the next run's reader what happened:

1. Every configured field exists. The query is built from them and nothing
   changes; the hypothesis was wrong and the log says the fields are fine,
   which narrows the cause to something else in the request.
2. A configured field is missing. It is named in the log, dropped from the
   query, and the rest still run. This is the case that gets the data back.
3. The metadata call itself fails. The query falls back to `outFields=*`,
   which asks for every field and therefore cannot name one the layer lacks.

The fallback matters more than the detection. An unknown field name is the one
failure mode this service is documented to have, and `*` sidesteps it entirely
at the cost of a slightly larger response for ~25 queries a month.
"""
from __future__ import annotations

import source_probe

#: Returned by resolve_out_fields when no field list could be established.
#: Asks the service for every field, which cannot name one it does not have.
ALL_FIELDS = '*'


def metadata_url(query_url):
    """Turn a .../FeatureServer/0/query URL into the layer's metadata URL.

    ArcGIS layer metadata lives at the layer root, which is the query URL with
    the trailing "/query" removed.
    """
    url = (query_url or '').rstrip('/')
    if url.lower().endswith('/query'):
        url = url[:-len('/query')]
    return url


def describe(query_url, requests_mod, headers=None, timeout=15, verify=True):
    """Fetch a layer's field names. Returns (status, detail, field_names).

    `field_names` is a list only when status is OK, matching the contract in
    source_probe: a failure never hands back data a caller could mistake for
    an answer.
    """
    url = metadata_url(query_url)
    try:
        r = requests_mod.get(url, params={'f': 'json'}, headers=headers or {},
                             timeout=timeout, verify=verify)
    except Exception as e:
        status, detail, _ = source_probe.classify_exception(e)
        return status, f"layer metadata: {detail}", []

    # Layer metadata carries "fields", not "features", and an ArcGIS error
    # body here is the same HTTP-200-with-an-error shape that hid the parcel
    # failure in the first place.
    status, detail, fields = source_probe.classify_response(r, data_key='fields')
    if status != source_probe.OK:
        return status, f"layer metadata: {detail}", []

    names = [f.get('name') for f in fields
             if isinstance(f, dict) and isinstance(f.get('name'), str)]
    if not names:
        return source_probe.BAD_JSON, 'layer metadata: no usable field names', []
    return source_probe.OK, f"{len(names)} fields", names


def resolve_out_fields(configured, available):
    """Match configured field names against what the layer really has.

    Returns (out_fields, resolved, missing).

    `out_fields` is the string to send as the `outFields` parameter.
    `resolved` maps each configured name to the layer's spelling of it.
    `missing` lists configured names the layer does not have.

    Matching is case-insensitive because ArcGIS is inconsistent about case
    between a service's published schema and its query responses, and a case
    difference alone should not cost us the field.

    With no field list (`available` empty or None) this returns ALL_FIELDS,
    because sending names that may not exist is the one request shape this
    service is documented to reject outright.
    """
    configured = [c for c in (configured or []) if c]
    if not available:
        return ALL_FIELDS, {}, []

    by_lower = {}
    for name in available:
        by_lower.setdefault(name.lower(), name)

    resolved, missing = {}, []
    for name in configured:
        actual = by_lower.get(name.lower())
        if actual is None:
            missing.append(name)
        else:
            resolved[name] = actual

    if not resolved:
        # Every configured field is gone. Asking for none of them would return
        # rows with no attributes, which is useless and would read as data.
        return ALL_FIELDS, {}, missing

    # Deduplicate while keeping the configured order.
    seen, ordered = set(), []
    for name in configured:
        actual = resolved.get(name)
        if actual and actual not in seen:
            seen.add(actual)
            ordered.append(actual)
    return ','.join(ordered), resolved, missing


def report_lines(label, status, detail, out_fields, resolved, missing,
                 available=None):
    """Render what the preflight established, for the run log.

    `available` is the layer's full field list. It is printed WHENEVER a
    configured field is missing, because without it the run tells you a field
    is gone but not what replaced it, which costs a whole extra run to find
    out. That happened on 2026-10-26: the preflight correctly reported that
    Acres_C no longer exists, the owner data came back, and the acreage stayed
    empty because nobody could see that the layer now calls it something else.
    """
    out = []
    if status != source_probe.OK:
        out.append(f"  [FAIL] {label} metadata unavailable ({status}): {detail}")
        out.append(f"  [WARN] Falling back to outFields={ALL_FIELDS!r}. The field "
                   f"names in the registry cannot be checked against the live "
                   f"layer this run.")
        return out

    out.append(f"  {label}: {detail}")
    if missing:
        out.append(f"  [FAIL] {label} does NOT have these configured field(s): "
                   f"{', '.join(missing)}")
        out.append(f"  [WARN] This is the documented cause of this service's "
                   f"'Failed to execute query' error. Update the registry's "
                   f"parcels.statewide_service to the layer's current names.")
        if available:
            out.append(f"  The layer's {len(available)} actual field(s), so the "
                       f"replacement can be picked without another run:")
            # Wrapped rather than one per line: 21 fields is a wall of log.
            row = []
            for name in available:
                row.append(name)
                if len(row) == 6:
                    out.append('    ' + ', '.join(row))
                    row = []
            if row:
                out.append('    ' + ', '.join(row))
    renamed = {k: v for k, v in (resolved or {}).items() if k != v}
    if renamed:
        pairs = ', '.join(f"{k} -> {v}" for k, v in renamed.items())
        out.append(f"  {label}: matched by case-insensitive name ({pairs})")
    out.append(f"  {label}: querying with outFields={out_fields}")
    return out
