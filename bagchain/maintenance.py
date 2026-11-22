"""Belt maintenance advisories, review paths and traceable work orders.

The RMS rule is deliberately unchanged and explicitly provisional: RMS only
opens an ``inspect`` note.  It can never authorise a bearing replacement, and
belt imbalance at the shaft frequency must not be recorded as end-of-life
bearing damage.  Confirmation needs either a pre-shutdown high-rate
oscilloscope buffer (settles history and warranty) or a current teardown with
micrographs (settles the current repair only).
"""
import json
from datetime import datetime, timezone
from .store import connect
from .spectrum_proof import high_rate_buffer_resolution

RMS_THRESHOLD = 0.4
PENDING = 'pending_review'
TENSION = 'tension_only'
REPLACE = 'replace_bearing'


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _next_version(db, motor_id):
    row = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM belt_advice_version WHERE motor_id=?',
                     (motor_id,)).fetchone()
    return row[0]


def advise(db, motor_id):
    """Latest RMS drives the provisional status; every call is versioned."""
    sample = db.execute('SELECT * FROM belt_sample WHERE motor_id=? ORDER BY rowid DESC LIMIT 1',
                        (motor_id,)).fetchone()
    status = 'inspect' if sample['rms_g'] >= RMS_THRESHOLD else 'run'
    version = _next_version(db, motor_id)
    db.execute(
        'INSERT INTO belt_advice_version(advice_id,motor_id,sample_id,version,status,reason,rms_g,created_seq) '
        'VALUES(?,?,?,?,?,?,?,?)',
        (f'{motor_id}-A{version}', motor_id, sample['id'], version, status, 'rms_threshold',
         sample['rms_g'], version))
    # The original single-row view stays in place for the baseline seed test.
    db.execute(
        'INSERT INTO belt_maintenance VALUES(?,?,?) ON CONFLICT(motor_id) DO UPDATE SET '
        'status=excluded.status,reason=excluded.reason',
        (motor_id, status, 'rms_threshold'))
    return {'motor_id': motor_id, 'status': status, 'version': version,
            'advice_id': f'{motor_id}-A{version}', 'sample_id': sample['id']}


def _motor(db, motor_id):
    row = db.execute('SELECT * FROM belt_motor WHERE id=?', (motor_id,)).fetchone()
    if row is None:
        raise ValueError('unknown_motor')
    return row


def _latest_sample(db, motor_id):
    sample = db.execute('SELECT * FROM belt_sample WHERE motor_id=? ORDER BY rowid DESC LIMIT 1',
                        (motor_id,)).fetchone()
    if sample is None:
        raise ValueError('no_sample')
    return sample


def _open_order_version(db, motor_id):
    row = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM belt_work_order WHERE motor_id=?',
                     (motor_id,)).fetchone()
    return row[0]


def _insert_order(db, motor, sample, order_type, review_kind, evidence_sha,
                  downtime_window, warranty_position, reason, band=None):
    version = _open_order_version(db, motor['id'])
    order_id = f'{motor["id"]}-WO{version}'
    db.execute(
        'INSERT INTO belt_work_order(order_id,motor_id,station,order_type,band,evidence_sample_id,'
        'review_kind,evidence_sha,downtime_window,warranty_position,reason,created_at,version) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (order_id, motor['id'], motor['station'], order_type, band, sample['id'], review_kind,
         evidence_sha, downtime_window, warranty_position, reason, _now(), version))
    return order_id


def _insert_review(db, motor_id, sample_id, kind, evidence_ref, resolution,
                   warranty_position, attributes):
    seq = db.execute('SELECT COALESCE(MAX(created_seq),0)+1 FROM belt_review').fetchone()[0]
    review_id = f'{motor_id}-{kind}-R{seq}'
    db.execute(
        'INSERT INTO belt_review(review_id,motor_id,sample_id,review_kind,evidence_ref,resolution,'
        'warranty_position,attributes,created_seq) VALUES(?,?,?,?,?,?,?,?,?)',
        (review_id, motor_id, sample_id, kind, evidence_ref, resolution, warranty_position,
         json.dumps(attributes, sort_keys=True), seq))
    return review_id


def request_review_order(db_path, motor_id):
    """Open the only order an RMS-only inspect can justify: still-to-verify.

    No station is marked as bearing-replaced without defect confirmation.
    """
    with connect(db_path) as db, db:
        motor = _motor(db, motor_id)
        sample = _latest_sample(db, motor_id)
        if sample['rms_g'] < RMS_THRESHOLD:
            raise ValueError('review_requires_inspect_advisory')
        order_id = _insert_order(
            db, motor, sample, PENDING, None, sample['source_sha'], None,
            'warranty_dispute_open',
            'rms_inspect_alias_ambiguous_needs_evidence')
    return {'motor_id': motor_id, 'order_id': order_id, 'order_type': PENDING}


def register_high_rate_buffer(db_path, motor_id, buffer_samples, buffer_hz, evidence_ref,
                              downtime_window, sample_id=None):
    """Review path 1: a synchronously stored pre-shutdown high-rate buffer.

    This buffer was captured during the same historical power-on window, so its
    verdict is attributable to that shutdown and can support a bearing quality
    warranty claim.  A different motor (e.g. M2 with another sample rate) must
    bring its own buffer; M1's de-aliasing result is never reused.
    """
    with connect(db_path) as db, db:
        motor = _motor(db, motor_id)
        sample = _latest_sample(db, motor_id) if sample_id is None else db.execute(
            'SELECT * FROM belt_sample WHERE id=? AND motor_id=?', (sample_id, motor_id)).fetchone()
        if sample is None:
            raise ValueError('sample_not_found')
        reading = high_rate_buffer_resolution(buffer_samples, buffer_hz, motor['rated_rpm'])
        attrs = {'buffer_hz': buffer_hz, 'evidence_ref': evidence_ref,
                 'tonal_frequency_hz': reading['tonal_frequency_hz'],
                 'tonal_band': reading['tonal_band'],
                 'bearing_band_energy': reading['bearing_band_energy'],
                 'shaft_band_energy': reading['shaft_band_energy'],
                 'covers_bearing_band': reading['covers_bearing_band']}
        if not reading['decidable']:
            review_id = _insert_review(db, motor_id, sample['id'], 'high_rate_buffer',
                                       evidence_ref, PENDING, 'warranty_dispute_open', attrs)
            order_id = _insert_order(db, motor, sample, PENDING, 'high_rate_buffer',
                                     sample['source_sha'], None, 'warranty_dispute_open',
                                     'buffer_inconclusive', reading['tonal_band'])
            return {'motor_id': motor_id, 'review_id': review_id, 'order_id': order_id,
                    'order_type': PENDING, 'verdict': reading['verdict']}
        if reading['verdict'] == 'bearing_replace_candidate':
            order_type, reason, warranty = (
                REPLACE, 'high_rate_buffer_confirms_bearing_band', 'bearing_warranty_claim_supported')
        else:
            order_type, reason, warranty = (
                TENSION, 'high_rate_buffer_confirms_shaft_imbalance', 'no_bearing_claim_belt_cause')
        review_id = _insert_review(db, motor_id, sample['id'], 'high_rate_buffer',
                                   evidence_ref, order_type, warranty, attrs)
        order_id = _insert_order(db, motor, sample, order_type, 'high_rate_buffer',
                                 sample['source_sha'], downtime_window, warranty, reason,
                                 reading['tonal_band'])
    return {'motor_id': motor_id, 'review_id': review_id, 'order_id': order_id,
            'order_type': order_type, 'verdict': reading['verdict'],
            'warranty_position': warranty}


def register_teardown(db_path, motor_id, micrograph_ref, bearing_damage_confirmed,
                      downtime_window, sample_id=None):
    """Review path 2: open the bearing raceway now and keep micrographs.

    The belt kept running after the original capture, so current wear marks
    cannot be backdated into the historical 200 Hz spectrum or a fault-onset
    claim.  This decides the current repair only; the historical warranty
    dispute stays explicitly open.
    """
    with connect(db_path) as db, db:
        motor = _motor(db, motor_id)
        sample = _latest_sample(db, motor_id) if sample_id is None else db.execute(
            'SELECT * FROM belt_sample WHERE id=? AND motor_id=?', (sample_id, motor_id)).fetchone()
        if sample is None:
            raise ValueError('sample_not_found')
        attrs = {'micrograph_ref': micrograph_ref,
                 'bearing_damage_confirmed': bool(bearing_damage_confirmed)}
        if bearing_damage_confirmed:
            order_type, resolution, reason = (
                REPLACE, REPLACE, 'current_raceway_damage_confirmed_by_teardown')
        else:
            order_type, resolution, reason = (
                TENSION, TENSION, 'teardown_finds_no_bearing_damage_tension_only')
        review_id = _insert_review(db, motor_id, sample['id'], 'teardown_microscopy',
                                   micrograph_ref, resolution,
                                   'historical_fault_onset_not_attributable_warranty_dispute_open',
                                   attrs)
        order_id = _insert_order(db, motor, sample, order_type, 'teardown_microscopy',
                                 sample['source_sha'], downtime_window,
                                 'current_repair_only_historical_warranty_dispute_open',
                                 reason)
    return {'motor_id': motor_id, 'review_id': review_id, 'order_id': order_id,
            'order_type': order_type,
            'warranty_position': 'current_repair_only_historical_warranty_dispute_open'}


def history(db_path, motor_id):
    """Full trace: original samples, versioned RMS advices, reviews and orders."""
    with connect(db_path) as db:
        _motor(db, motor_id)
        samples = [dict(r) for r in db.execute(
            'SELECT id,sample_hz,rms_g,time_base_id,seq_index,source_sha,n_points FROM belt_sample '
            'WHERE motor_id=? ORDER BY rowid', (motor_id,))]
        advices = [dict(r) for r in db.execute(
            'SELECT advice_id,sample_id,version,status,reason,rms_g FROM belt_advice_version '
            'WHERE motor_id=? ORDER BY version', (motor_id,))]
        reviews = [dict(r) for r in db.execute(
            'SELECT review_id,sample_id,review_kind,evidence_ref,resolution,warranty_position '
            'FROM belt_review WHERE motor_id=? ORDER BY created_seq', (motor_id,))]
        orders = [dict(r) for r in db.execute(
            'SELECT order_id,station,order_type,band,evidence_sample_id,review_kind,evidence_sha,'
            'downtime_window,warranty_position,reason,version FROM belt_work_order '
            'WHERE motor_id=? ORDER BY version', (motor_id,))]
    return {'motor_id': motor_id, 'samples': samples, 'rms_advices': advices,
            'reviews': reviews, 'work_orders': orders}
