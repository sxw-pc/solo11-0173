"""Acceptance tests for the belt vibration aliasing proof and maintenance flow.

The same 200 captured points are simultaneously consistent with adjustable belt
imbalance (process A, tension only) and bearing rolling-element damage
(process B, replace bearing).  The tests pin down that no implementation may
invent the missing high-frequency evidence, and that the two permitted review
paths create different warranty and maintenance scopes.
"""
import json
import math
from pathlib import Path

import pytest

from bagchain.belt import import_motor, import_sample
from bagchain.maintenance import (advise, history, register_high_rate_buffer,
                                  register_teardown, request_review_order)
from bagchain.spectrum_proof import (RMS_INSPECT_THRESHOLD, alias_equivalence,
                                     analyze_capture, classify_peak,
                                     detect_saturation, high_rate_buffer_resolution,
                                     one_sided_spectrum, rms,
                                     validate_time_base_segments)
from bagchain.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures' / 'belt-alias.json'
RMS_AMP = math.sqrt(2) * 0.5
SAMPLE_HZ = 200
N_POINTS = 200


@pytest.fixture()
def alias_doc():
    return json.loads(FIX.read_text(encoding='utf-8'))


@pytest.fixture()
def seeded(tmp_path, alias_doc):
    db = tmp_path / 'belt.sqlite'
    motor_file = tmp_path / 'motor.json'
    sample_file = tmp_path / 'sample.json'
    motor_file.write_text(json.dumps(alias_doc['motor']))
    sample_file.write_text(json.dumps(alias_doc['sample']))
    import_motor(db, motor_file)
    result = import_sample(db, sample_file)
    return db, result, alias_doc


# --- Acceptance 1: the real origin of the 0.5 g -> inspect advisory ---------

def test_rms_threshold_is_the_origin_of_inspect_and_is_versioned(seeded):
    db, result, _ = seeded
    assert result['status'] == 'inspect'
    assert result['version'] == 1
    with connect(db) as c:
        row = c.execute('SELECT status, reason FROM belt_maintenance WHERE motor_id=?',
                        ('M1',)).fetchone()
        assert row['status'] == 'inspect' and row['reason'] == 'rms_threshold'
        advice = c.execute('SELECT * FROM belt_advice_version ORDER BY version').fetchall()
        assert len(advice) == 1 and advice[0]['rms_g'] == 0.5
        assert advice[0]['status'] == 'inspect'
    # The threshold itself is what flips the status: 0.399 g stays "run".
    assert (0.5 >= RMS_INSPECT_THRESHOLD) and (0.399 < RMS_INSPECT_THRESHOLD)


def test_below_threshold_advisory_is_run(tmp_path):
    db = tmp_path / 'b.sqlite'
    m = tmp_path / 'm.json'
    m.write_text(json.dumps({'motor_id': 'M9', 'station': 'SORT-B', 'rated_rpm': 1200}))
    s = tmp_path / 's.json'
    s.write_text(json.dumps({'sample_id': 'S9', 'motor_id': 'M9', 'sample_hz': 200,
                             'rms_g': 0.3, 'samples': [0.1] * 10}))
    import_motor(db, m)
    assert import_sample(db, s)['status'] == 'run'


# --- Acceptance 2: A and B share all 200 points but differ in maintenance ---

def test_processes_a_and_b_are_pointwise_identical_over_200_points(alias_doc):
    proof = alias_equivalence()
    samples_a = proof['process_a']['samples']
    samples_b = proof['process_b']['samples']
    assert len(samples_a) == len(samples_b) == N_POINTS
    assert proof['pointwise_equal']
    assert proof['max_pointwise_abs_diff'] < 1e-12
    # Algebraic check for every integer sample index, not just the stored list:
    # sin(2*pi*180*n/200 + pi) = -sin(2*pi*(1 - 0.1n)*2*pi form) folds to A.
    for n in range(N_POINTS):
        t = n / SAMPLE_HZ
        a = RMS_AMP * math.sin(2 * math.pi * 20 * t)
        b = RMS_AMP * math.sin(2 * math.pi * 180 * t + math.pi)
        assert a == pytest.approx(b, abs=1e-12)
        assert a == pytest.approx(samples_a[n], abs=1e-12)
    assert rms(samples_a) == pytest.approx(0.5)
    assert rms(samples_b) == pytest.approx(0.5)
    # Fixture on disk is exactly this shared record.
    assert alias_doc['sample']['samples'] == pytest.approx(samples_a, abs=1e-12)
    assert alias_doc['alias_proof']['pointwise_equal'] is True


def test_same_points_yield_different_maintenance_but_only_one_visible_peak(seeded):
    db, _, _ = seeded
    proof = alias_equivalence()
    reading = analyze_capture(proof['process_a']['samples'], SAMPLE_HZ, 1200)
    # Only physically reachable bins (0..100 Hz) are ever emitted.
    assert max(p['hz'] for p in reading['spectrum']) <= SAMPLE_HZ / 2.0
    visible = [p for p in reading['peaks'] if p['amplitude_g'] > 1e-6]
    assert len(visible) == 1
    assert visible[0]['hz'] == pytest.approx(20.0)
    assert visible[0]['band'] == 'shaft'
    # 180 Hz is retained only as an indistinguishable hidden candidate.
    hidden = reading['alias_ambiguous'][0]['hidden_true_hz']
    assert 180.0 in hidden and 20.0 not in hidden
    assert reading['decidable'] is False
    assert reading['verdict'] == 'indeterminate'
    # An RMS-only inspect opens a pending order; it cannot be a bearing order.
    order = request_review_order(db, 'M1')
    assert order['order_type'] == 'pending_review'
    with connect(db) as c:
        kinds = [r['order_type'] for r in c.execute('SELECT order_type FROM belt_work_order')]
    assert 'replace_bearing' not in kinds
    assert proof['process_a']['maintenance'] == 'tension_only'
    assert proof['process_b']['maintenance'] == 'replace_bearing'


def test_no_transform_manufactures_high_frequency_evidence(alias_doc):
    samples = alias_doc['sample']['samples']
    padded = one_sided_spectrum(samples, SAMPLE_HZ, pad_to_power_of_two=True)
    exact = one_sided_spectrum(samples, SAMPLE_HZ, pad_to_power_of_two=False)
    # Neither view ever emits a bin above Nyquist: no fake high-frequency peak.
    for spectrum in (padded, exact):
        assert max(p['hz'] for p in spectrum) <= SAMPLE_HZ / 2.0
    # The exact-bin DFT is the evidentiary reading: tone at 20 Hz, numerical
    # zero everywhere above 90 Hz (the 180 Hz evidence cannot be synthesised).
    top = max(exact, key=lambda p: p['amplitude_g'])
    assert top['hz'] == pytest.approx(20.0)
    assert top['amplitude_g'] == pytest.approx(RMS_AMP, rel=1e-9)
    assert all(p['amplitude_g'] < 1e-9 for p in exact if p['hz'] > 90.0)
    # Padding changes display granularity only and never leaves the band.
    assert max(padded, key=lambda p: p['amplitude_g'])['hz'] <= 30.0


# --- Acceptance 3: time base, filter and dropped-frame limits ---------------

def test_shared_time_base_rejects_per_segment_rate_tuning():
    base = {'time_base_id': 'TB1', 'sample_hz': 200, 'samples': [0.1] * 8}
    report = validate_time_base_segments([
        {**base, 'seq_index': 0}, {**base, 'seq_index': 2}])
    assert report['TB1']['dropped_frames'] == 1
    with pytest.raises(ValueError, match='sample_rate_conflict_within_time_base'):
        validate_time_base_segments([
            {**base, 'seq_index': 0},
            {**base, 'seq_index': 1, 'sample_hz': 300}])
    with pytest.raises(ValueError, match='duplicate_seq_index'):
        validate_time_base_segments([{**base, 'seq_index': 0}, {**base, 'seq_index': 0}])


def test_import_enforces_time_base_and_records_filter_gain_tach(seeded):
    db, _, doc = seeded
    with connect(db) as c:
        sample = c.execute('SELECT * FROM belt_sample WHERE id=?',
                           (doc['sample']['sample_id'],)).fetchone()
        assert sample['time_base_id'] == 'TB-M1-20261122-01'
        assert sample['channel_gain'] == 1.0
        assert json.loads(sample['aa_filter'])['cutoff_hz'] == 100.0
        assert json.loads(sample['tach_pulses'])['measured_shaft_hz'] == 20.0
        assert sample['source_sha'] and len(sample['source_sha']) == 64
        evidence = c.execute('SELECT * FROM belt_evidence').fetchall()
        assert len(evidence) == 1 and evidence[0]['kind'] == 'vibration_sample'


def test_undersampling_dropped_frames_and_saturation_emit_no_fake_peak():
    # Undersampled bearing tone: 180 Hz at 200 Hz folds to 20 Hz, nothing above.
    folded = [RMS_AMP * math.sin(2 * math.pi * 180 * n / SAMPLE_HZ + math.pi)
              for n in range(N_POINTS)]
    reading = analyze_capture(folded, SAMPLE_HZ, 1200)
    assert reading['decidable'] is False
    assert all(p['hz'] <= SAMPLE_HZ / 2.0 for p in reading['spectrum'])
    # Saturated channel: spectrum stays feasible but no verdict is signed.
    saturated, count = detect_saturation([RMS_AMP] * 6 + [0.0] * 14)
    assert saturated and count == 6
    sat_reading = analyze_capture([RMS_AMP] * 6 + [0.0] * 194, SAMPLE_HZ, 1200)
    assert sat_reading['saturated'] is True
    assert sat_reading['verdict'] == 'indeterminate'
    # Dropped frames are surfaced as a count, never interpolated into peaks.
    segs = validate_time_base_segments([
        {'time_base_id': 'TB1', 'seq_index': 0, 'sample_hz': 200, 'samples': folded[:10]},
        {'time_base_id': 'TB1', 'seq_index': 3, 'sample_hz': 200, 'samples': folded[:10]}])
    assert segs['TB1']['dropped_frames'] == 2


# --- Acceptance 4: the two review paths have different warranty scope -------

def test_high_rate_buffer_separates_a_and_b_and_supports_warranty(seeded):
    db, _, _ = seeded
    buffer_b = [RMS_AMP * math.sin(2 * math.pi * 180 * n / 1000 + math.pi)
                for n in range(1000)]
    review = register_high_rate_buffer(
        db, 'M1', buffer_b, 1000, 'osc-buffer-sha-1',
        '2026-11-22T10:00:00Z/2026-11-22T12:00:00Z')
    assert review['order_type'] == 'replace_bearing'
    assert review['warranty_position'] == 'bearing_warranty_claim_supported'
    # Same buffer for process A (20 Hz) must instead authorise tension only.
    buffer_a = [RMS_AMP * math.sin(2 * math.pi * 20 * n / 1000) for n in range(1000)]
    review_a = register_high_rate_buffer(
        db, 'M1', buffer_a, 1000, 'osc-buffer-sha-2',
        '2026-11-22T10:00:00Z/2026-11-22T12:00:00Z')
    assert review_a['order_type'] == 'tension_only'
    assert review_a['warranty_position'] == 'no_bearing_claim_belt_cause'


def test_teardown_decides_current_repair_but_keeps_history_disputed(seeded):
    db, _, _ = seeded
    review = register_teardown(
        db, 'M1', 'micrograph-sha-1', True,
        '2026-11-23T02:00:00Z/2026-11-23T04:00:00Z')
    assert review['order_type'] == 'replace_bearing'
    assert review['warranty_position'] == \
        'current_repair_only_historical_warranty_dispute_open'
    clean = register_teardown(
        db, 'M1', 'micrograph-sha-2', False,
        '2026-11-23T02:00:00Z/2026-11-23T04:00:00Z')
    assert clean['order_type'] == 'tension_only'
    h = history(db, 'M1')
    teardowns = [r for r in h['reviews'] if r['review_kind'] == 'teardown_microscopy']
    assert all('not_attributable' in r['warranty_position'] for r in teardowns)


def test_review_requires_motor_specific_evidence_no_cross_motor_transfer(tmp_path, seeded):
    db, _, _ = seeded
    # M2 samples at a different rate; M1's de-aliasing outcome cannot apply.
    m2 = tmp_path / 'm2.json'
    m2.write_text(json.dumps({'motor_id': 'M2', 'station': 'SORT-C', 'rated_rpm': 1500}))
    s2 = tmp_path / 's2.json'
    s2.write_text(json.dumps({'sample_id': 'S2', 'motor_id': 'M2', 'sample_hz': 400,
                              'rms_g': 0.45, 'samples': [0.1] * 16}))
    import_motor(db, m2)
    import_sample(db, s2)
    with pytest.raises(ValueError, match='sample_not_found'):
        register_high_rate_buffer(db, 'M2', [0.0] * 1000, 1000, 'x',
                                  '2026-11-22T10:00:00Z/2026-11-22T12:00:00Z',
                                  sample_id='M1-S200-01')
    # Insufficient Nyquist (500 Hz buffer cannot reach the 500 Hz band edge).
    weak = high_rate_buffer_resolution([0.01 * math.sin(2 * math.pi * 120 * n / 500)
                                        for n in range(500)], 500, 1200)
    assert weak['covers_bearing_band'] is False
    assert weak['verdict'] == 'indeterminate'


# --- Acceptance 5: traceability of samples, advisories, orders, downtime ----

def test_full_history_links_sample_advice_review_and_downtime(seeded):
    db, _, doc = seeded
    request_review_order(db, 'M1')
    register_high_rate_buffer(
        db, 'M1', [RMS_AMP * math.sin(2 * math.pi * 180 * n / 1000 + math.pi)
                   for n in range(1000)], 1000, 'osc-buffer-sha-1',
        '2026-11-22T10:00:00Z/2026-11-22T12:00:00Z')
    h = history(db, 'M1')
    assert h['samples'][0]['id'] == doc['sample']['sample_id']
    assert h['samples'][0]['source_sha']
    assert [a['status'] for a in h['rms_advices']] == ['inspect']
    assert h['reviews'][0]['review_kind'] == 'high_rate_buffer'
    order = h['work_orders'][-1]
    assert order['order_type'] == 'replace_bearing'
    assert order['evidence_sample_id'] == doc['sample']['sample_id']
    assert order['evidence_sha'] == h['samples'][0]['source_sha']
    assert order['downtime_window'] == '2026-11-22T10:00:00Z/2026-11-22T12:00:00Z'
    # The pending order is retained alongside the confirmed order: nothing is
    # overwritten, and no station other than M1's has an order.
    assert [o['order_type'] for o in h['work_orders']][0] == 'pending_review'
    with connect(db) as c:
        stations = {r['station'] for r in c.execute('SELECT DISTINCT station FROM belt_work_order')}
    assert stations == {'SORT-A'}


def test_late_advice_versions_keep_old_rms_note(tmp_path):
    db = tmp_path / 'b.sqlite'
    m = tmp_path / 'm.json'
    m.write_text(json.dumps({'motor_id': 'M7', 'station': 'SORT-A', 'rated_rpm': 1200}))
    import_motor(db, m)
    first = tmp_path / 's1.json'
    first.write_text(json.dumps({'sample_id': 'S1', 'motor_id': 'M7', 'sample_hz': 200,
                                 'rms_g': 0.5, 'samples': [0.5] * 8}))
    second = tmp_path / 's2.json'
    second.write_text(json.dumps({'sample_id': 'S2', 'motor_id': 'M7', 'sample_hz': 200,
                                  'rms_g': 0.2, 'samples': [0.2] * 8}))
    import_sample(db, first)
    import_sample(db, second)
    h = history(db, 'M7')
    assert [(a['version'], a['status'], a['sample_id']) for a in h['rms_advices']] == [
        (1, 'inspect', 'S1'), (2, 'run', 'S2')]
    with connect(db) as c:
        assert c.execute('SELECT status FROM belt_maintenance WHERE motor_id=?',
                         ('M7',)).fetchone()[0] == 'run'
    # Calling advise directly also versions rather than mutating history.
    with connect(db) as c:
        out = advise(c, 'M7')
        assert out['version'] == 3
