"""
Tell a parcel service that returned NOTHING apart from one that FAILED.

WHY THIS EXISTS
---------------
On 2026-10-12 the first CI run of this pipeline reported
`Parcel matches (statewide + county GIS): 0` and filled `current_owner` for
0 of 19 candidates, against 22 of 22 in the previous output. The cause could
not be read off the log, because the code could not tell the two cases apart,
and that is the defect this module closes.

Two separate paths were collapsing failure into absence:

1. `except Exception: return None, None, None` returns exactly what "no parcel
   here" returns, so a timeout, a DNS failure and an SSL error all read as a
   site with no parcel.

2. The worse one, because it never reaches that `except` at all:
   `requests.get()` does NOT raise on 4xx or 5xx, so `r.json().get("features",
   [])` on an HTTP 400 parses the ERROR body, finds no "features" key, and
   returns an empty list. ArcGIS compounds this by returning many errors as
   HTTP 200 with an `{"error": {...}}` body. This script's own comment records
   that WVGIS "errors out entirely (400 Failed to execute query)" on some
   query shapes, so this is the expected failure of this exact service and it
   was invisible.

An absence measurement licenses only an absence claim. A service that failed
has measured nothing, and a pipeline that reports it as zero parcels is
asserting something it did not observe.

WHAT THIS DOES NOT DO
---------------------
It does not retry, change any query, or alter which parcels are found. It only
classifies what came back and counts it, so the next run's log answers the
question this one could not. Behaviour for a genuinely successful query is
unchanged.
"""
from __future__ import annotations

# A query that reached the service and got parcels back.
OK = 'ok'
# Reached the service, which answered correctly that there is nothing here.
# This is the ONLY status that licenses "this site has no parcel".
EMPTY = 'empty'
# Everything below is a FAILURE. None of them say anything about the site.
HTTP_ERROR = 'http_error'          # 4xx or 5xx
SERVICE_ERROR = 'service_error'    # HTTP 200 carrying an ArcGIS {"error": ...}
BAD_JSON = 'bad_json'              # 200, but the body is not JSON (HTML, proxy page)
TRANSPORT_ERROR = 'transport_error'  # timeout, DNS, TLS, connection reset

FAILURES = (HTTP_ERROR, SERVICE_ERROR, BAD_JSON, TRANSPORT_ERROR)


def classify_response(response):
    """Classify a parcel-service HTTP response.

    Returns (status, detail, features). `features` is a list only for OK;
    it is empty for every other status, so a caller that ignores the status
    still cannot mistake a failure for data.
    """
    status_code = getattr(response, 'status_code', None)
    if status_code is not None and not (200 <= status_code < 300):
        return HTTP_ERROR, f"HTTP {status_code}", []

    try:
        body = response.json()
    except Exception as e:
        # A proxy block page or an HTML error is not JSON. Record the type
        # rather than the whole body, which can be a megabyte of markup.
        return BAD_JSON, f"{type(e).__name__}: body is not JSON", []

    if not isinstance(body, dict):
        return BAD_JSON, f"expected a JSON object, got {type(body).__name__}", []

    # ArcGIS returns many real errors as HTTP 200 with an error object. This
    # is the case that made the failure invisible, so it is checked before
    # anything looks for "features".
    err = body.get('error')
    if err:
        if isinstance(err, dict):
            msg = err.get('message') or err.get('description') or str(err)
            code = err.get('code')
            detail = f"ArcGIS error {code}: {msg}" if code else f"ArcGIS error: {msg}"
        else:
            detail = f"ArcGIS error: {err}"
        return SERVICE_ERROR, detail, []

    feats = body.get('features')
    if feats is None:
        # No "features" key and no "error" key. Not a shape this service is
        # documented to return, so it is not treated as an empty result.
        keys = ', '.join(sorted(body)[:6]) or 'none'
        return BAD_JSON, f"response has no 'features' key (keys: {keys})", []
    if not isinstance(feats, list):
        return BAD_JSON, f"'features' is {type(feats).__name__}, expected a list", []
    if not feats:
        return EMPTY, 'service returned zero parcels', []
    return OK, f"{len(feats)} parcel(s)", feats


def classify_exception(exc):
    """Classify an exception raised before any response came back."""
    return TRANSPORT_ERROR, f"{type(exc).__name__}: {exc}", []


class OutcomeCounter:
    """Tally query outcomes and render a summary that cannot be misread.

    The point is the FAILED line. A run that reports "0 parcel matches" while
    every request failed has measured nothing, and the summary says so in those
    words rather than leaving the reader to infer it from a zero.
    """

    ORDER = (OK, EMPTY, HTTP_ERROR, SERVICE_ERROR, BAD_JSON, TRANSPORT_ERROR)

    def __init__(self, label='parcel service'):
        self.label = label
        self.counts = {s: 0 for s in self.ORDER}
        self.first_detail = {}

    def record(self, status, detail=''):
        if status not in self.counts:
            raise ValueError(f"unknown status {status!r}")
        self.counts[status] += 1
        # Keep the FIRST example of each failure, which is the one that shows
        # what went wrong before any retry or rate limit muddies it.
        if status in FAILURES and status not in self.first_detail and detail:
            self.first_detail[status] = detail

    @property
    def total(self):
        return sum(self.counts.values())

    @property
    def failed(self):
        return sum(self.counts[s] for s in FAILURES)

    @property
    def answered(self):
        """Queries the service actually answered: OK plus a genuine empty."""
        return self.counts[OK] + self.counts[EMPTY]

    def all_failed(self):
        return self.total > 0 and self.failed == self.total

    def lines(self):
        """Render the summary as a list of lines, ready to print."""
        out = [f"  {self.label}: {self.total} queries, "
               f"{self.counts[OK]} with parcels, {self.counts[EMPTY]} genuinely empty, "
               f"{self.failed} FAILED"]
        for s in FAILURES:
            if self.counts[s]:
                d = self.first_detail.get(s, '')
                out.append(f"    [FAIL] {s}: {self.counts[s]}" + (f" -- first: {d}" if d else ''))
        if self.all_failed():
            out.append(f"  [WARN] EVERY {self.label} query failed. The zero above is "
                       f"a measurement of the service, not of the sites. Acreage and "
                       f"owner columns are unfilled for this reason, not because the "
                       f"parcels are absent.")
        elif self.failed and self.failed >= self.answered:
            out.append(f"  [WARN] {self.failed} of {self.total} {self.label} queries "
                       f"failed, so the counts above understate coverage by an "
                       f"unknown amount.")
        return out

    def report(self, printer=print):
        for line in self.lines():
            printer(line)
