"""Import conveyor motor records and vibration evidence.

The baseline RMS import is preserved byte-for-byte in behaviour: a newest
sample with RMS >= 0.4 g still yields ``inspect`` and anything lower ``run``.
Newer captures may additionally carry sequence numbers, anti-alias filter
configuration, channel gain, synchronised tachometer pulses and multiple
segments sharing one recorder time base.
"""
import json
from pathlib import Path
from .store import canonical, connect, digest
from .spectrum_proof import rms, validate_time_base_segments


def import_motor(db_path, file):
    doc = json.loads(Path(file).read_text())
    with connect(db_path) as db, db:
        if not doc['motor_id'] or doc['rated_rpm'] <= 0:
            raise ValueError('invalid_motor')
        db.execute('INSERT INTO belt_motor VALUES(?,?,?,?)',
                   (doc['motor_id'], doc['station'], doc['rated_rpm'],
                    doc.get('time_base_id')))
    return {'motor_id': doc['motor_id']}


def _motor_row(db, motor_id):
    row = db.execute('SELECT * FROM belt_motor WHERE id=?', (motor_id,)).fetchone()
    if row is None:
        raise ValueError('unknown_motor')
    return row


def import_sample(db_path, file):
    raw = Path(file).read_text()
    doc = json.loads(raw)
    with connect(db_path) as db, db:
        motor = _motor_row(db, doc['motor_id'])
        if doc['sample_hz'] <= 0 or doc['rms_g'] < 0:
            raise ValueError('invalid_sample')
        samples = doc.get('samples') or []
        if samples:
            rms(samples)  # rejects empty/non-finite captures
        sha = digest(raw.encode('utf-8'))
        time_base_id = doc.get('time_base_id') or motor['time_base_id']
        tach = doc.get('tach_pulses')
        aa_filter = doc.get('aa_filter')
        gain = doc.get('channel_gain')
        seq_index = doc.get('seq_index', 0)
        db.execute(
            'INSERT INTO belt_sample(id,motor_id,sample_hz,rms_g,raw,time_base_id,'
            'seq_index,tach_pulses,aa_filter,channel_gain,source_sha,n_points) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (doc['sample_id'], doc['motor_id'], doc['sample_hz'], doc['rms_g'], raw,
             time_base_id, seq_index,
             None if tach is None else canonical(tach),
             None if aa_filter is None else canonical(aa_filter),
             gain, sha, len(samples)))
        segments = doc.get('segments')
        if segments:
            payload = [
                {'time_base_id': s.get('time_base_id', time_base_id),
                 'seq_index': s['seq_index'], 'sample_hz': s['sample_hz'],
                 'samples': s['samples'],
                 'sensor_limit_g': s.get('sensor_limit_g')}
                for s in segments]
            # One shared time base per power-on: no per-segment rate tuning.
            validate_time_base_segments(payload)
            for i, (s) in enumerate(segments):
                checked = payload[i]
                db.execute(
                    'INSERT INTO belt_segment(segment_id,sample_id,time_base_id,seq_index,'
                    'sample_hz,n_points,aa_filter,channel_gain,dropped_frames,saturated,samples) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (f"{doc['sample_id']}-{checked['seq_index']}", doc['sample_id'],
                     checked['time_base_id'], checked['seq_index'], checked['sample_hz'],
                     len(checked['samples']),
                     None if s.get('aa_filter') is None else canonical(s['aa_filter']),
                     s.get('channel_gain', gain), s.get('dropped_frames', 0),
                     int(checked['sensor_limit_g'] is not None and
                         any(abs(x) >= checked['sensor_limit_g'] for x in checked['samples'])),
                     canonical(checked['samples'])))
        db.execute(
            'INSERT INTO belt_evidence(evidence_id,motor_id,sample_id,kind,source_sha,payload,recorded_seq) '
            'VALUES(?,?,?,?,?,?,?)',
            (doc['sample_id'], doc['motor_id'], doc['sample_id'], 'vibration_sample', sha,
             canonical(doc),
             db.execute('SELECT COALESCE(MAX(recorded_seq),0)+1 FROM belt_evidence').fetchone()[0]))
        from .maintenance import advise
        return advise(db, doc['motor_id'])
