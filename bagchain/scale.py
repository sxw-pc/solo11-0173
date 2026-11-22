"""Capture a check-in load cell's force and the originals needed to prove bag mass."""
import json
from pathlib import Path
from .store import all_rows,connect,epoch,one


def import_scale(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if not doc['scale_id'] or not doc['station']:raise ValueError('invalid_scale')
        db.execute('INSERT INTO checkin_scale VALUES(?,?,?)',
                   (doc['scale_id'],doc['station'],raw))
    return {'scale_id':doc['scale_id']}


def import_force(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if not db.execute('SELECT 1 FROM checkin_scale WHERE id=?',(doc['scale_id'],)).fetchone():
            raise ValueError('unknown_scale')
        if doc['force_n']<0:raise ValueError('invalid_force')
        db.execute('INSERT INTO scale_sample VALUES(?,?,?,?,?,?)',
                   (doc['sample_id'],doc['scale_id'],doc['bag_id'],doc['at'],doc['force_n'],raw))
        from .weight_fee import advise
        result=advise(db,doc['bag_id'])
        from .mass_proof import ensure_pending,reconstruct_bag
        ensure_pending(db,doc['bag_id'])
        reconstruct_bag(db,doc['bag_id'])
        return result


def import_calibration(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if not one(db,'SELECT 1 FROM checkin_scale WHERE id=?',(doc['scale_id'],)):raise ValueError('unknown_scale')
        if not doc['batch'] or doc['gain']<=0:raise ValueError('invalid_calibration')
        old=one(db,'SELECT raw FROM scale_calibration WHERE cal_id=?',(doc['cal_id'],))
        if old:
            if old['raw']!=raw:raise ValueError('calibration_conflict')
            return {'cal_id':doc['cal_id'],'status':'replayed'}
        db.execute('INSERT INTO scale_calibration VALUES(?,?,?,?,?,?)',
                   (doc['cal_id'],doc['scale_id'],doc['batch'],doc['gain'],doc['at'],raw))
        from .mass_proof import reconstruct_scale
        return {'cal_id':doc['cal_id'],'status':'imported','tickets':reconstruct_scale(db,doc['scale_id'])}


def import_tare(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if not one(db,'SELECT 1 FROM checkin_scale WHERE id=?',(doc['scale_id'],)):raise ValueError('unknown_scale')
        if doc['tare_n']<0 or doc.get('drift_n',0.0)<0:raise ValueError('invalid_tare')
        old=one(db,'SELECT raw FROM scale_tare WHERE tare_id=?',(doc['tare_id'],))
        if old:
            if old['raw']!=raw:raise ValueError('tare_conflict')
            return {'tare_id':doc['tare_id'],'status':'replayed'}
        db.execute('INSERT INTO scale_tare VALUES(?,?,?,?,?,?,?)',
                   (doc['tare_id'],doc['scale_id'],doc['at'],doc['tare_n'],doc.get('drift_n',0.0),
                    1 if doc.get('reproducible',True) else 0,raw))
        from .mass_proof import reconstruct_scale
        return {'tare_id':doc['tare_id'],'status':'imported','tickets':reconstruct_scale(db,doc['scale_id'])}


def import_accel(db_path,file):
    """Shared platform inertial series: one time base per scale, overlapping windows rejected."""
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if not one(db,'SELECT 1 FROM checkin_scale WHERE id=?',(doc['scale_id'],)):raise ValueError('unknown_scale')
        if not doc['shift']:raise ValueError('invalid_accel_series')
        points=[[p[0],float(p[1])] for p in doc['points']]
        if not points:raise ValueError('invalid_accel_series')
        points.sort(key=lambda p:epoch(p[0]))
        old=one(db,'SELECT raw FROM platform_accel WHERE series_id=?',(doc['series_id'],))
        if old:
            if old['raw']!=raw:raise ValueError('accel_conflict')
            return {'series_id':doc['series_id'],'status':'replayed'}
        lo,hi=epoch(points[0][0]),epoch(points[-1][0])
        for series in all_rows(db,'SELECT points FROM platform_accel WHERE scale_id=?',(doc['scale_id'],)):
            window=sorted(epoch(p[0]) for p in json.loads(series['points']))
            if lo<=window[-1] and hi>=window[0]:raise ValueError('accel_series_overlap')
        db.execute('INSERT INTO platform_accel VALUES(?,?,?,?,?)',
                   (doc['series_id'],doc['scale_id'],doc['shift'],json.dumps(points),raw))
        from .mass_proof import reconstruct_scale
        return {'series_id':doc['series_id'],'status':'imported','tickets':reconstruct_scale(db,doc['scale_id'])}


def import_position(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        sample=one(db,'SELECT * FROM scale_sample WHERE id=?',(doc['sample_id'],))
        if not sample:raise ValueError('unknown_sample')
        if sample['bag_id']!=doc['bag_id']:raise ValueError('position_bag_mismatch')
        old=one(db,'SELECT raw FROM sample_position WHERE position_id=?',(doc['position_id'],))
        if old:
            if old['raw']!=raw:raise ValueError('position_conflict')
            return {'position_id':doc['position_id'],'status':'replayed'}
        db.execute('INSERT INTO sample_position VALUES(?,?,?,?,?,?)',
                   (doc['position_id'],doc['sample_id'],doc['bag_id'],doc['at'],
                    1 if doc['corner_on_scale'] else 0,raw))
        from .mass_proof import reconstruct_bag
        return {'position_id':doc['position_id'],'status':'imported','tickets':reconstruct_bag(db,doc['bag_id'])}


def import_overload(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        sample=one(db,'SELECT * FROM scale_sample WHERE id=?',(doc['sample_id'],))
        if not sample:raise ValueError('unknown_sample')
        if sample['scale_id']!=doc['scale_id']:raise ValueError('overload_scale_mismatch')
        old=one(db,'SELECT raw FROM sensor_overload WHERE overload_id=?',(doc['overload_id'],))
        if old:
            if old['raw']!=raw:raise ValueError('overload_conflict')
            return {'overload_id':doc['overload_id'],'status':'replayed'}
        db.execute('INSERT INTO sensor_overload VALUES(?,?,?,?,?)',
                   (doc['overload_id'],doc['sample_id'],doc['scale_id'],doc['at'],raw))
        from .mass_proof import reconstruct_bag
        return {'overload_id':doc['overload_id'],'status':'imported','tickets':reconstruct_bag(db,sample['bag_id'])}


def import_reweigh(db_path,file):
    """Static verification bench weighing; only a bag with unchanged seal proves anything."""
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if doc['weight_kg']<=0:raise ValueError('invalid_reweigh')
        old=one(db,'SELECT raw FROM static_reweigh WHERE reweigh_id=?',(doc['reweigh_id'],))
        if old:
            if old['raw']!=raw:raise ValueError('reweigh_conflict')
            return {'reweigh_id':doc['reweigh_id'],'status':'replayed'}
        db.execute('INSERT INTO static_reweigh VALUES(?,?,?,?,?,?)',
                   (doc['reweigh_id'],doc['bag_id'],doc['at'],doc['weight_kg'],
                    1 if doc['seal_intact'] else 0,raw))
        from .mass_proof import ensure_pending,reconstruct_bag
        ensure_pending(db,doc['bag_id'])
        return {'reweigh_id':doc['reweigh_id'],'status':'imported','tickets':reconstruct_bag(db,doc['bag_id'])}
