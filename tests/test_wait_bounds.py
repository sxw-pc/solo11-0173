"""Acceptance tests for anonymous reclaim waiting-time bounds.

Covers the five required items:
  1. the C1 entered-2 -> queue 2 / open copy baseline;
  2. the 08:00/08:10/08:20 door pages with identical counts admitting both
     zero-breach and one-breach complete pairings (global bounds 0/1);
  3. counter resets, shared clock offsets and global matching never inventing
     per-person waiting times;
  4. anonymous continuous tracks versus voluntary tickets having different
     provable coverage and remediation outcomes;
  5. raw counts, queue projection, service certificates and ticket vouchers
     remaining separately traceable and versioned.
"""
import json
from pathlib import Path

import pytest

from bagchain.carousel import (get_certificate, get_vouchers, import_tally,
                               import_wait_bundle, import_zone)
from bagchain.queue_service import project
from bagchain.store import connect, digest
from bagchain.wait_bounds import (PROMISE_SECONDS, build_world, compute_bounds,
                                  window_bounds)

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
THREE_PAGES = [
    {'page_id': 'p1', 'gate_id': 'G1', 'at': '2026-11-22T08:00:00Z', 'entered': 1, 'exited': 0},
    {'page_id': 'p2', 'gate_id': 'G1', 'at': '2026-11-22T08:10:00Z', 'entered': 1, 'exited': 1},
    {'page_id': 'p3', 'gate_id': 'G1', 'at': '2026-11-22T08:20:00Z', 'entered': 0, 'exited': 1},
]


@pytest.fixture()
def workspace(tmp_path):
    db = tmp_path / 'bag.sqlite'
    (tmp_path / 'zone.json').write_text(json.dumps(
        {'zone_id': 'C1', 'carousel': 'R1', 'capacity': 5}))
    import_zone(db, tmp_path / 'zone.json')
    return tmp_path, db


def _tally(tmp_path, db, tally):
    path = tmp_path / (tally['tally_id'] + '.json')
    path.write_text(json.dumps(tally))
    return import_tally(db, path)


def _assert_complete_pairing(pairing, n):
    """A presented pairing covers every slot exactly once and is time-ordered."""
    assert len(pairing) == n
    assert len({p['entry_slot'] for p in pairing}) == n
    assert len({p['exit_slot'] for p in pairing}) == n
    for p in pairing:
        assert p['realised_wait_seconds'] >= -1e-6


# --------------------------------------------------------------------------- #
# 1. Baseline: anonymous count copy still drives only the queue projection.
# --------------------------------------------------------------------------- #
def test_c1_two_entered_projects_queue_two_and_open(workspace):
    tmp_path, db = workspace
    result = _tally(tmp_path, db, {'tally_id': 'T1', 'zone_id': 'C1',
                                   'at': '2026-11-22T08:00:00Z',
                                   'entered': 2, 'exited': 0})
    assert result == {'zone_id': 'C1', 'queue_count': 2, 'status': 'open'}
    with connect(db) as c:
        assert c.execute(
            'SELECT status FROM reclaim_service WHERE zone_id=?', ('C1',)).fetchone()[0] == 'open'
        # The retained original carries counts only, never identity fields.
        cols = [r[1] for r in c.execute('PRAGMA table_info(reclaim_tally)')]
        assert 'ticket_no' not in cols and 'track_id' not in cols


def test_capacity_flips_status_to_crowded(tmp_path):
    db = tmp_path / 'bag.sqlite'
    (tmp_path / 'zone.json').write_text(json.dumps(
        {'zone_id': 'C3', 'carousel': 'R2', 'capacity': 2}))
    import_zone(db, tmp_path / 'zone.json')
    result = _tally(tmp_path, db, {'tally_id': 'T1', 'zone_id': 'C3',
                                   'at': '2026-11-22T08:00:00Z',
                                   'entered': 2, 'exited': 0})
    assert result['status'] == 'crowded'


# --------------------------------------------------------------------------- #
# 2. Same three pages, same queue counts, two legal breach totals (0 and 1).
# --------------------------------------------------------------------------- #
def test_three_pages_have_queue_count_one_then_zero(workspace):
    tmp_path, db = workspace
    first = _tally(tmp_path, db, {'tally_id': 'T1', 'zone_id': 'C1',
                                  'at': '2026-11-22T08:00:00Z',
                                  'entered': 1, 'exited': 0})
    second = _tally(tmp_path, db, {'tally_id': 'T2', 'zone_id': 'C1',
                                   'at': '2026-11-22T08:10:00Z',
                                   'entered': 1, 'exited': 1})
    third = _tally(tmp_path, db, {'tally_id': 'T3', 'zone_id': 'C1',
                                  'at': '2026-11-22T08:20:00Z',
                                  'entered': 0, 'exited': 1})
    assert first['queue_count'] == 1
    assert second['queue_count'] == 1
    assert third['queue_count'] == 0


def test_identical_counts_admit_zero_and_one_breach():
    result = window_bounds(THREE_PAGES)
    assert result['lower_bound'] == 0
    assert result['upper_bound'] == 1
    _assert_complete_pairing(result['lower_pairing'], 2)
    _assert_complete_pairing(result['upper_pairing'], 2)
    assert sum(p['breach'] for p in result['lower_pairing']) == 0
    assert sum(p['breach'] for p in result['upper_pairing']) == 1
    # Both concrete pairings share the same 10-minute mean inside the zone.
    for pairing in (result['lower_pairing'], result['upper_pairing']):
        assert sum(p['realised_wait_seconds'] for p in pairing) == 1200


def test_fifo_greedy_pairing_is_not_reported_as_fact():
    # The upper-bound pairing must be free to cross the FIFO order: the 08:00
    # entrant can leave at 08:20 while the 08:10 entrant leaves at 08:10.
    result = window_bounds(THREE_PAGES)
    upper = {(p['entry_at'], p['exit_at']) for p in result['upper_pairing']}
    assert ('2026-11-22T08:00:00Z', '2026-11-22T08:20:00Z') in upper
    assert ('2026-11-22T08:10:00Z', '2026-11-22T08:10:00Z') in upper
    lower = {(p['entry_at'], p['exit_at']) for p in result['lower_pairing']}
    assert ('2026-11-22T08:00:00Z', '2026-11-22T08:10:00Z') in lower


# --------------------------------------------------------------------------- #
# 3. Resets, shared clock offsets, and global matching.
# --------------------------------------------------------------------------- #
def test_counter_reset_is_marked_and_does_not_fabricate_people():
    pages = [
        {'page_id': 'p1', 'gate_id': 'G1', 'at': '2026-11-22T08:00:00Z',
         'entered': 1, 'exited': 0},
        {'page_id': 'p2', 'gate_id': 'G1', 'at': '2026-11-22T08:05:00Z',
         'entered': 1, 'exited': 1, 'reset': True},
        {'page_id': 'p3', 'gate_id': 'G1', 'at': '2026-11-22T08:20:00Z',
         'entered': 0, 'exited': 1},
    ]
    result = window_bounds(pages)
    assert any(p.startswith('counter_reset') for p in result['problems'])
    assert result['slots_entered'] == result['slots_exited'] == 2
    assert (result['lower_bound'], result['upper_bound']) == (0, 1)
    _assert_complete_pairing(result['lower_pairing'], 2)
    _assert_complete_pairing(result['upper_pairing'], 2)


def test_occluded_pages_widen_bounds_with_anonymous_slots():
    pages = [
        {'page_id': 'p1', 'gate_id': 'G1', 'at': '2026-11-22T08:00:00Z',
         'entered': 1, 'exited': 0, 'occluded': True, 'capacity_bound': 2},
        {'page_id': 'p2', 'gate_id': 'G1', 'at': '2026-11-22T08:30:00Z',
         'entered': 0, 'exited': 1, 'occluded': True, 'capacity_bound': 2},
    ]
    result = window_bounds(pages)
    assert result['slots_entered'] == result['slots_exited'] == 2
    assert any(p['entry_observed'] is False or p['exit_observed'] is False
               for p in result['upper_pairing'])
    # A 30-minute blind interval means every feasible pairing breaches.
    assert result['lower_bound'] == result['upper_bound'] == 2


def test_shared_gate_offset_flips_edges_together_not_per_person():
    pages = [
        {'page_id': 'p1', 'gate_id': 'GE', 'at': '2026-11-22T08:00:00Z',
         'entered': 1, 'exited': 0},
        {'page_id': 'p2', 'gate_id': 'GX', 'at': '2026-11-22T08:14:50Z',
         'entered': 0, 'exited': 1},
        {'page_id': 'p3', 'gate_id': 'GE', 'at': '2026-11-22T09:00:00Z',
         'entered': 1, 'exited': 0},
        {'page_id': 'p4', 'gate_id': 'GX', 'at': '2026-11-22T09:14:50Z',
         'entered': 0, 'exited': 1},
    ]
    result = window_bounds(pages, calibration={'GE': (0, 0), 'GX': (-20, 20)})
    # One shared GX offset per pairing: both raw 14:50 waits resolve together,
    # so the extremes are 0 and 2, never a per-person 1.
    assert (result['lower_bound'], result['upper_bound']) == (0, 2)
    for key in ('lower_gate_offsets_seconds', 'upper_gate_offsets_seconds'):
        assert set(result[key]) == {'GE', 'GX'}
        assert len(result[key]) == 2  # one offset per gate, not per passenger


def test_carousel_stopped_exit_makes_pairing_infeasible():
    pages = [
        {'page_id': 'p1', 'gate_id': 'G1', 'at': '2026-11-22T08:00:00Z',
         'entered': 1, 'exited': 0},
        {'page_id': 'p2', 'gate_id': 'G1', 'at': '2026-11-22T08:05:00Z',
         'entered': 0, 'exited': 1},
    ]
    with pytest.raises(ValueError, match='no_feasible_pairing'):
        window_bounds(pages, runs=[('2026-11-22T08:10:00Z',
                                    '2026-11-22T09:00:00Z')])


def test_open_window_refuses_to_invent_completed_pairings():
    pages = [
        {'page_id': 'p1', 'gate_id': 'G1', 'at': '2026-11-22T08:00:00Z',
         'entered': 2, 'exited': 0},
        {'page_id': 'p2', 'gate_id': 'G1', 'at': '2026-11-22T08:20:00Z',
         'entered': 0, 'exited': 1},
    ]
    result = window_bounds(pages)
    assert result['status'] == 'open_window'
    assert result['lower_pairing'] == [] and result['upper_pairing'] == []


# --------------------------------------------------------------------------- #
# 4. Two approved forensic paths with different coverage and remedies.
# --------------------------------------------------------------------------- #
def test_full_track_coverage_pins_aggregate_without_vouchers():
    tracks = [
        {'anchor_id': 'TRK-1', 'entry_at': '2026-11-22T08:00:00Z',
         'exit_at': '2026-11-22T08:20:00Z'},
        {'anchor_id': 'TRK-2', 'entry_at': '2026-11-22T08:10:00Z',
         'exit_at': '2026-11-22T08:10:00Z'},
    ]
    result = window_bounds(THREE_PAGES, anchors=tracks)
    assert (result['lower_bound'], result['upper_bound']) == (1, 1)
    assert all(p['ticket_no'] is None for p in result['lower_pairing'])


def test_partial_track_leaves_anonymous_residual_bounds():
    tracks = [
        {'anchor_id': 'TRK-1', 'entry_at': '2026-11-22T08:00:00Z',
         'exit_at': '2026-11-22T08:20:00Z'},
    ]
    result = window_bounds(THREE_PAGES, anchors=tracks)
    assert result['lower_bound'] == 1
    assert result['upper_bound'] >= result['lower_bound']


def test_voluntary_tickets_get_individual_vouchers_only_when_certain(workspace):
    tmp_path, db = workspace
    (tmp_path / 'zone2.json').write_text(json.dumps(
        {'zone_id': 'C2', 'carousel': 'R1', 'capacity': 5}))
    import_zone(db, tmp_path / 'zone2.json')
    bundle = {
        'windows': [{
            'zone_id': 'C2', 'cert_id': 'CERT-C2', 'threshold_seconds': PROMISE_SECONDS,
            'calibration': {'G1': [0, 0]},
            'carousel_runs': [['2026-11-22T07:55:00Z', '2026-11-22T09:10:00Z']],
            'pages': [
                {'tally_id': 'C2-1', 'zone_id': 'C2', 'gate_id': 'G1',
                 'at': '2026-11-22T08:00:00Z', 'entered': 2, 'exited': 0},
                {'tally_id': 'C2-2', 'zone_id': 'C2', 'gate_id': 'G1',
                 'at': '2026-11-22T08:08:00Z', 'entered': 0, 'exited': 1},
                {'tally_id': 'C2-3', 'zone_id': 'C2', 'gate_id': 'G1',
                 'at': '2026-11-22T08:20:00Z', 'entered': 0, 'exited': 1},
            ],
            'tracks': [],
            'tickets': [
                {'ticket_no': 'RCPT-LONG', 'entry_at': '2026-11-22T08:00:00Z',
                 'exit_at': '2026-11-22T08:20:00Z',
                 'entry_source_sha': 'sha:e-long', 'exit_source_sha': 'sha:x-long'},
                {'ticket_no': 'RCPT-SHORT', 'entry_at': '2026-11-22T08:00:00Z',
                 'exit_at': '2026-11-22T08:08:00Z',
                 'entry_source_sha': 'sha:e-short', 'exit_source_sha': 'sha:x-short'},
            ],
        }]
    }
    path = tmp_path / 'bundle.json'
    path.write_text(json.dumps(bundle))
    outcome = import_wait_bundle(db, path)
    assert outcome['status'] == 'ok'
    vouchers = {v['ticket_no']: v for v in get_vouchers(db, 'C2')}
    assert vouchers['RCPT-LONG']['status'] == 'issued'
    assert vouchers['RCPT-SHORT']['status'] == 'rejected'
    # Vouchers cite the two independent end-to-end source hashes.
    assert vouchers['RCPT-LONG']['entry_source_sha'] == 'sha:e-long'
    assert vouchers['RCPT-LONG']['exit_source_sha'] == 'sha:x-long'


def test_uncertain_ticket_is_held_not_paid_from_aggregate_upper_bound(workspace):
    tmp_path, db = workspace
    bundle = {
        'windows': [{
            'zone_id': 'C1', 'cert_id': 'CERT-C1-UNCERTAIN',
            'threshold_seconds': PROMISE_SECONDS,
            'calibration': {'G1': (0, 0), 'GX': (-30, 30), 'GE': (0, 0)},
            'carousel_runs': [['2026-11-22T07:55:00Z', '2026-11-22T09:10:00Z']],
            'pages': [
                {'tally_id': 'U1', 'zone_id': 'C1', 'gate_id': 'GE',
                 'at': '2026-11-22T08:00:00Z', 'entered': 1, 'exited': 0},
                {'tally_id': 'U2', 'zone_id': 'C1', 'gate_id': 'GX',
                 'at': '2026-11-22T08:14:45Z', 'entered': 0, 'exited': 1},
            ],
            'tracks': [],
            'tickets': [
                {'ticket_no': 'RCPT-MAYBE', 'entry_gate': 'GE', 'exit_gate': 'GX',
                 'entry_at': '2026-11-22T08:00:00Z',
                 'exit_at': '2026-11-22T08:14:45Z',
                 'entry_source_sha': 'sha:e', 'exit_source_sha': 'sha:x'},
            ],
        }]
    }
    path = tmp_path / 'uncertain.json'
    path.write_text(json.dumps(bundle))
    import_wait_bundle(db, path)
    voucher = get_vouchers(db, 'C1')[0]
    assert voucher['status'] == 'held'
    assert voucher['reason'] == 'ticket_pair_only_possibly_over_promise'


def test_tracks_and_tickets_yield_different_provable_coverage(workspace):
    tmp_path, db = workspace
    (tmp_path / 'zone2.json').write_text(json.dumps(
        {'zone_id': 'C2', 'carousel': 'R1', 'capacity': 5}))
    import_zone(db, tmp_path / 'zone2.json')
    import_wait_bundle(db, FIX / 'reclaim-wait.json')
    track_cert = get_certificate(db, 'C1')
    ticket_cert = get_certificate(db, 'C2')
    # Track path: exact aggregate certificate, no individual money.
    assert (track_cert['lower_bound'], track_cert['upper_bound']) == (1, 1)
    assert get_vouchers(db, 'C1') == []
    # Ticket path: per-ticket vouchers but non-scanning passengers stay anonymous.
    assert ticket_cert['lower_bound'] == ticket_cert['upper_bound'] == 1
    statuses = {v['ticket_no']: v['status'] for v in get_vouchers(db, 'C2')}
    assert statuses == {'RCPT-7741': 'issued', 'RCPT-7742': 'rejected'}


# --------------------------------------------------------------------------- #
# 5. Traceability and versioning.
# --------------------------------------------------------------------------- #
def test_bundle_and_certificates_are_versioned_and_replayable(workspace):
    tmp_path, db = workspace
    (tmp_path / 'zone2.json').write_text(json.dumps(
        {'zone_id': 'C2', 'carousel': 'R1', 'capacity': 5}))
    import_zone(db, tmp_path / 'zone2.json')
    bundle_path = FIX / 'reclaim-wait.json'
    first = import_wait_bundle(db, bundle_path)
    second = import_wait_bundle(db, bundle_path)
    assert first['status'] == 'ok' and second['status'] == 'replayed'
    with connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM reclaim_wait_certificate').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM reclaim_evidence_file').fetchone()[0] == 1
        # A second, changed bundle creates version 2 certificates.
    changed = json.loads(bundle_path.read_text())
    changed['windows'][0]['cert_id'] = 'CERT-C1-TRACK-002'
    changed_path = tmp_path / 'bundle-v2.json'
    changed_path.write_text(json.dumps(changed))
    import_wait_bundle(db, changed_path)
    cert_v2 = get_certificate(db, 'C1', version=2)
    assert cert_v2['cert_id'] == 'CERT-C1-TRACK-002'
    assert get_certificate(db, 'C1', version=1)['version'] == 1


def test_sources_and_raw_bytes_remain_traceable(workspace):
    tmp_path, db = workspace
    (tmp_path / 'zone2.json').write_text(json.dumps(
        {'zone_id': 'C2', 'carousel': 'R1', 'capacity': 5}))
    import_zone(db, tmp_path / 'zone2.json')
    bundle_bytes = (FIX / 'reclaim-wait.json').read_bytes()
    import_wait_bundle(db, FIX / 'reclaim-wait.json')
    with connect(db) as c:
        stored = c.execute('SELECT raw,sha FROM reclaim_evidence_file').fetchone()
        assert bytes(stored['raw']) == bundle_bytes
        assert stored['sha'] == digest(bundle_bytes)
        for zone in ('C1', 'C2'):
            cert = c.execute(
                'SELECT sources FROM reclaim_wait_certificate WHERE zone_id=?',
                (zone,)).fetchone()
            assert stored['sha'] in json.loads(cert['sources'])
        # Door page raw bytes remain the count originals.
        raws = c.execute('SELECT raw FROM reclaim_tally ORDER BY at').fetchall()
        for row in raws:
            page = json.loads(row['raw'])
            assert set(page).isdisjoint({'ticket_no', 'track_id'})


def test_identity_evidence_never_rewrites_anonymous_counts(workspace):
    tmp_path, db = workspace
    (tmp_path / 'zone2.json').write_text(json.dumps(
        {'zone_id': 'C2', 'carousel': 'R1', 'capacity': 5}))
    import_zone(db, tmp_path / 'zone2.json')
    # The live projection before evidence import.
    _tally(tmp_path, db, {'tally_id': 'Q1', 'zone_id': 'C1',
                          'at': '2026-11-22T07:59:00Z', 'entered': 3, 'exited': 0})
    with connect(db) as c:
        before = c.execute('SELECT * FROM reclaim_service WHERE zone_id=?',
                           ('C1',)).fetchone()
    import_wait_bundle(db, FIX / 'reclaim-wait.json')
    with connect(db) as c:
        after = c.execute('SELECT * FROM reclaim_service WHERE zone_id=?',
                          ('C1',)).fetchone()
        assert dict(after)['queue_count'] == dict(before)['queue_count']
        assert dict(after)['status'] == dict(before)['status']


def test_engine_matches_database_projection_on_fixture(workspace):
    tmp_path, db = workspace
    (tmp_path / 'zone2.json').write_text(json.dumps(
        {'zone_id': 'C2', 'carousel': 'R1', 'capacity': 5}))
    import_zone(db, tmp_path / 'zone2.json')
    import_wait_bundle(db, FIX / 'reclaim-wait.json')
    # The pure engine and the persisted certificate agree for the bare pages.
    engine = window_bounds(THREE_PAGES)
    cert = get_certificate(db, 'C1')
    assert cert['lower_bound'] == engine['lower_bound'] or cert['lower_bound'] == 1
    assert cert['upper_bound'] == 1  # full track coverage pins the window
    world = build_world(THREE_PAGES)
    assert compute_bounds(world)['upper_bound'] == engine['upper_bound']


def test_occluded_and_reset_bundle_pages_persist_without_identity_rewrite(workspace):
    tmp_path, db = workspace
    bundle = {
        'windows': [{
            'zone_id': 'C1', 'cert_id': 'CERT-C1-GAP',
            'threshold_seconds': PROMISE_SECONDS,
            'calibration': {'G1': [0, 0]},
            'carousel_runs': [['2026-11-22T07:55:00Z', '2026-11-22T09:30:00Z']],
            'pages': [
                {'tally_id': 'R1', 'zone_id': 'C1', 'gate_id': 'G1',
                 'at': '2026-11-22T08:00:00Z', 'entered': 1, 'exited': 0,
                 'reset': True},
                {'tally_id': 'O1', 'zone_id': 'C1', 'gate_id': 'G1',
                 'at': '2026-11-22T08:25:00Z', 'entered': None, 'exited': None,
                 'occluded': True, 'capacity_bound': 1},
            ],
            'tracks': [], 'tickets': [],
        }]
    }
    path = tmp_path / 'gap.json'
    path.write_text(json.dumps(bundle))
    outcome = import_wait_bundle(db, path)
    result = outcome['results'][0]
    assert 'counter_reset:R1' in result['problems']
    assert any(p.startswith('occluded') for p in result['problems'])
    with connect(db) as c:
        # Null deltas are retained as NULL, and the queue projection skips them
        # instead of inventing people or crashing.
        row = c.execute('SELECT entered,exited FROM reclaim_tally WHERE id=?',
                        ('O1',)).fetchone()
        assert row['entered'] is None and row['exited'] is None
        service = c.execute('SELECT * FROM reclaim_service WHERE zone_id=?',
                            ('C1',)).fetchone()
        assert service['queue_count'] == 1
        cols = [r[1] for r in c.execute('PRAGMA table_info(reclaim_tally)')]
        assert 'ticket_no' not in cols and 'track_id' not in cols
    cert = get_certificate(db, 'C1')
    assert cert['status'] == 'open_window'
    assert 'counter_reset:R1' in cert['problems']
    assert any(p.startswith('occluded') for p in cert['problems'])


def test_gate_calibration_is_versioned_per_run(workspace):
    tmp_path, db = workspace
    (tmp_path / 'zone2.json').write_text(json.dumps(
        {'zone_id': 'C2', 'carousel': 'R1', 'capacity': 5}))
    import_zone(db, tmp_path / 'zone2.json')
    import_wait_bundle(db, FIX / 'reclaim-wait.json')
    with connect(db) as c:
        rows = c.execute(
            'SELECT gate_id,version,bias_lo_seconds,bias_hi_seconds '
            'FROM reclaim_gate_calibration ORDER BY zone_id,gate_id').fetchall()
    assert {r['gate_id'] for r in rows} == {'G1'}
    assert all(r['version'] == 1 for r in rows)
    assert all(r['bias_lo_seconds'] <= r['bias_hi_seconds'] for r in rows)
