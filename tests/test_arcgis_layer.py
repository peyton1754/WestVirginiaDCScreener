"""
Tests for the ArcGIS layer preflight.

The case this exists for: a layer that no longer has `Acres_C`. The live
service cannot be reached from the environment this was written in, so the
fixtures are built from the ArcGIS metadata shape rather than from a captured
response, and that limit is stated rather than hidden. What they do pin is the
decision logic, which is the part that would otherwise be guesswork.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import arcgis_layer as al  # noqa: E402
import source_probe as sp  # noqa: E402

QUERY_URL = ('https://services.wvgis.wvu.edu/arcgis/rest/services/'
             'Planning_Cadastre/WV_Parcels/MapServer/0/query')
LAYER_URL = QUERY_URL[:-len('/query')]

CONFIGURED = ['FullOwnerName', 'Acres_C']


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raises=None):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class FakeRequests:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def layer_with(*names):
    return FakeResponse(200, {
        'name': 'WV_Parcels',
        'type': 'Feature Layer',
        'fields': [{'name': n, 'type': 'esriFieldTypeString'} for n in names],
    })


# --- metadata_url ---------------------------------------------------------

def test_metadata_url_strips_the_query_suffix():
    assert al.metadata_url(QUERY_URL) == LAYER_URL


def test_metadata_url_is_case_insensitive_and_tolerates_a_trailing_slash():
    assert al.metadata_url(QUERY_URL + '/') == LAYER_URL
    assert al.metadata_url(LAYER_URL + '/QUERY') == LAYER_URL


def test_metadata_url_leaves_a_layer_root_alone():
    assert al.metadata_url(LAYER_URL) == LAYER_URL


# --- describe -------------------------------------------------------------

def test_describe_returns_the_layers_field_names():
    req = FakeRequests(layer_with('OBJECTID', 'FullOwnerName', 'Acres_C'))
    status, detail, names = al.describe(QUERY_URL, req)
    assert status == sp.OK
    assert names == ['OBJECTID', 'FullOwnerName', 'Acres_C']
    assert '3 fields' in detail


def test_describe_asks_the_layer_root_not_the_query_endpoint():
    req = FakeRequests(layer_with('OBJECTID'))
    al.describe(QUERY_URL, req)
    url, kw = req.calls[0]
    assert url == LAYER_URL, "querying /query for metadata returns the wrong shape"
    assert kw['params'] == {'f': 'json'}


def test_describe_treats_an_arcgis_error_body_as_a_failure():
    """The same HTTP-200-with-an-error shape that hid the parcel failure."""
    r = FakeResponse(200, {'error': {'code': 400, 'message': 'Invalid URL'}})
    status, detail, names = al.describe(QUERY_URL, FakeRequests(r))
    assert status in sp.FAILURES
    assert names == []
    assert 'metadata' in detail


@pytest.mark.parametrize('code', [403, 404, 500])
def test_describe_treats_an_http_error_as_a_failure(code):
    r = FakeResponse(code, {'fields': [{'name': 'OBJECTID'}]})
    status, detail, names = al.describe(QUERY_URL, FakeRequests(r))
    assert names == [], "a failed metadata call handed back field names"
    # Assert the SPECIFIC status and that the detail carries the code. Just
    # `status in FAILURES` was too weak: a mutation that dropped the early
    # return still ended in a failure status, but a generic bad_json one, so
    # the run log would have lost the real reason the call failed.
    assert status == sp.HTTP_ERROR, f"lost the reason, reported {status}"
    assert str(code) in detail, f"the detail does not name the HTTP code: {detail!r}"


def test_describe_treats_a_transport_failure_as_a_failure():
    status, detail, names = al.describe(QUERY_URL, FakeRequests(TimeoutError('timed out')))
    assert status == sp.TRANSPORT_ERROR
    assert 'TimeoutError' in detail
    assert names == []


def test_describe_refuses_metadata_carrying_no_usable_names():
    r = FakeResponse(200, {'fields': [{'alias': 'no name key'}, 'not a dict']})
    status, detail, names = al.describe(QUERY_URL, FakeRequests(r))
    assert status == sp.BAD_JSON
    assert names == []
    assert 'field names' in detail


# --- resolve_out_fields ---------------------------------------------------

def test_every_configured_field_present_sends_exactly_those():
    out, resolved, missing = al.resolve_out_fields(
        CONFIGURED, ['OBJECTID', 'FullOwnerName', 'Acres_C', 'Shape__Area'])
    assert out == 'FullOwnerName,Acres_C'
    assert missing == []
    assert resolved == {'FullOwnerName': 'FullOwnerName', 'Acres_C': 'Acres_C'}


def test_a_renamed_field_is_named_and_dropped_rather_than_sent():
    """The hypothesis for the 2026-10-26 failure: the layer no longer has
    Acres_C. Sending it is the documented way to get 'Failed to execute
    query', so the remaining field is queried and the missing one is named."""
    out, resolved, missing = al.resolve_out_fields(
        CONFIGURED, ['OBJECTID', 'FullOwnerName', 'GIS_ACRES'])
    assert missing == ['Acres_C']
    assert out == 'FullOwnerName', "the missing field is still being sent"
    assert 'Acres_C' not in out


def test_case_differences_do_not_cost_us_the_field():
    out, resolved, missing = al.resolve_out_fields(
        CONFIGURED, ['fullownername', 'ACRES_C'])
    assert missing == []
    assert out == 'fullownername,ACRES_C', "must send the LAYER's spelling"
    assert resolved == {'FullOwnerName': 'fullownername', 'Acres_C': 'ACRES_C'}


def test_no_field_list_falls_back_to_all_fields():
    """With metadata unavailable, sending names that may not exist is the one
    shape this service is documented to reject outright."""
    for available in ([], None):
        out, resolved, missing = al.resolve_out_fields(CONFIGURED, available)
        assert out == al.ALL_FIELDS
        assert resolved == {} and missing == []


def test_every_field_missing_falls_back_to_all_fields_rather_than_asking_for_none():
    """An empty outFields returns rows with no attributes, which is useless
    and would read as data rather than as a problem."""
    out, _resolved, missing = al.resolve_out_fields(CONFIGURED, ['OBJECTID', 'SHAPE'])
    assert out == al.ALL_FIELDS
    assert sorted(missing) == ['Acres_C', 'FullOwnerName']


def test_duplicate_configured_names_are_sent_once():
    out, _r, _m = al.resolve_out_fields(
        ['Acres_C', 'Acres_C', 'FullOwnerName'], ['Acres_C', 'FullOwnerName'])
    assert out == 'Acres_C,FullOwnerName'


def test_empty_and_none_configured_names_are_ignored():
    out, _r, missing = al.resolve_out_fields(
        ['FullOwnerName', '', None], ['FullOwnerName'])
    assert out == 'FullOwnerName'
    assert missing == []


# --- report_lines ---------------------------------------------------------

def test_a_missing_field_is_reported_loudly_and_names_the_remedy():
    out, resolved, missing = al.resolve_out_fields(CONFIGURED, ['FullOwnerName'])
    text = '\n'.join(al.report_lines('WVGIS parcels', sp.OK, '1 fields',
                                     out, resolved, missing))
    assert '[FAIL]' in text
    assert 'Acres_C' in text
    assert 'Failed to execute query' in text, "must connect this to the error seen"
    assert 'statewide_service' in text, "must say where to fix it"


def test_unavailable_metadata_reports_the_fallback():
    text = '\n'.join(al.report_lines('WVGIS parcels', sp.HTTP_ERROR,
                                     'layer metadata: HTTP 404',
                                     al.ALL_FIELDS, {}, []))
    assert '[FAIL]' in text and 'HTTP 404' in text
    assert "outFields='*'" in text


def test_a_clean_preflight_is_quiet():
    out, resolved, missing = al.resolve_out_fields(CONFIGURED, CONFIGURED)
    text = '\n'.join(al.report_lines('WVGIS parcels', sp.OK, '2 fields',
                                     out, resolved, missing))
    assert '[FAIL]' not in text and '[WARN]' not in text
    assert 'outFields=FullOwnerName,Acres_C' in text


def test_a_case_rename_is_disclosed_rather_than_silently_applied():
    out, resolved, missing = al.resolve_out_fields(CONFIGURED, ['FULLOWNERNAME', 'Acres_C'])
    text = '\n'.join(al.report_lines('WVGIS parcels', sp.OK, '2 fields',
                                     out, resolved, missing))
    assert 'FullOwnerName -> FULLOWNERNAME' in text
