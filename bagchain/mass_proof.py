"""Confirmed bag mass from force, shared platform acceleration, and scale originals.

F = m*(g + a): a single force reading never determines mass while the platform
acceleration is unknown. Acceleration enters only through the shared per-shift
inertial series (one time base for every bag on the segment) or a sealed static
reweigh (a = 0 by instrument class). Unreliable readings keep a feasible mass
range and stop automatic charging instead of inventing a point mass.
"""
import json
from .store import all_rows,epoch,one
from .weight_fee import settle

G=9.8
ACCEL_ENVELOPE=4.9  # platform vertical acceleration bound when no inertial original exists, m/s^2

def _interp(points,at):
    """Shared time base: interpolate the shift's acceleration series at a sample time."""
    t=epoch(at);xs=sorted((epoch(p[0]),float(p[1])) for p in points)
    if t<xs[0][0] or t>xs[-1][0]:return None
    for (t0,a0),(t1,a1) in zip(xs,xs[1:]):
        if t0<=t<=t1:return a0 if t1==t0 else a0+(a1-a0)*(t-t0)/(t1-t0)
    return xs[-1][1]

def _latest_before(rows,at):
    picked=None
    for row in rows:
        if row['at']<=at:picked=row
    return picked

def _dynamic(db,bag_id):
    """Latest force sample combined with calibration, tare, position, overload and shared accel."""
    sample=one(db,'SELECT * FROM scale_sample WHERE bag_id=? ORDER BY at DESC LIMIT 1',(bag_id,))
    if not sample:return None
    scale=sample['scale_id'];issues=[]
    cal=_latest_before(all_rows(db,'SELECT * FROM scale_calibration WHERE scale_id=? ORDER BY at',(scale,)),sample['at'])
    gain=cal['gain'] if cal else 1.0
    if not cal:issues.append('calibration_missing')
    tare=_latest_before(all_rows(db,'SELECT * FROM scale_tare WHERE scale_id=? ORDER BY at',(scale,)),sample['at'])
    if not tare:
        issues.append('tare_missing');lo_n,hi_n=0.0,sample['force_n']
    else:
        lo_n=hi_n=sample['force_n']-tare['tare_n']
        if not tare['reproducible']:
            issues.append('tare_not_reproducible');lo_n-=tare['drift_n'];hi_n+=tare['drift_n']
    pos=one(db,'SELECT * FROM sample_position WHERE sample_id=?',(sample['id'],))
    if not pos:issues.append('position_missing');hi_n=None
    elif not pos['corner_on_scale']:issues.append('corner_off_scale');hi_n=None
    if one(db,'SELECT 1 FROM sensor_overload WHERE sample_id=?',(sample['id'],)):
        issues.append('force_saturated');hi_n=None
    accel=None;ref=None
    for series in all_rows(db,'SELECT * FROM platform_accel WHERE scale_id=?',(scale,)):
        value=_interp(json.loads(series['points']),sample['at'])
        if value is not None:accel,ref=value,series['series_id'];break
    if accel is None:issues.append('accel_unknown');d_lo,d_hi=G-ACCEL_ENVELOPE,G+ACCEL_ENVELOPE
    else:d_lo=d_hi=G+accel
    lo=lo_n*gain/d_hi
    hi=None if hi_n is None else hi_n*gain/d_lo
    confirmed=None if issues else lo_n*gain/d_lo
    return {'issues':issues,'lo':lo,'hi':hi,'confirmed':confirmed,
            'ref':ref or sample['id'],'at':sample['at'],'method':'imu_reconstruct'}

def _reweigh(db,bag_id):
    """Static verification bench: trusted only while the bag seal is unchanged."""
    rows=all_rows(db,'SELECT * FROM static_reweigh WHERE bag_id=? ORDER BY at',(bag_id,))
    if not rows:return None
    sealed=[r for r in rows if r['seal_intact']]
    rw=sealed[-1] if sealed else rows[-1]
    if not rw['seal_intact']:
        return {'issues':['seal_changed_reweigh_not_retroactive'],'lo':None,'hi':None,'confirmed':None,
                'ref':rw['reweigh_id'],'at':rw['at'],'method':'static_reweigh'}
    return {'issues':[],'lo':rw['weight_kg'],'hi':rw['weight_kg'],'confirmed':rw['weight_kg'],
            'ref':rw['reweigh_id'],'at':rw['at'],'method':'static_reweigh'}

def _attempt(db,bag_id):
    """Try to confirm the bag's mass once; a settled bag is never re-billed or rewritten."""
    if one(db,'SELECT 1 FROM fee_settlement WHERE bag_id=?',(bag_id,)):return None
    dyn=_dynamic(db,bag_id);rw=_reweigh(db,bag_id)
    if not dyn and not rw:return None
    pick=None
    for cand in (dyn,rw):
        if cand and not cand['issues'] and cand['confirmed'] is not None:pick=cand;break
    if pick is None:
        base=dyn or rw
        pick={'issues':list(dyn['issues'] if dyn else [])+list(rw['issues'] if rw else []),
              'lo':dyn['lo'] if dyn else None,'hi':dyn['hi'] if dyn else None,
              'confirmed':None,'ref':base['ref'],'at':base['at'],'method':'unresolved'}
    version=db.execute('SELECT COUNT(*) FROM mass_ticket WHERE bag_id=?',(bag_id,)).fetchone()[0]+1
    db.execute('INSERT INTO mass_ticket VALUES(?,?,?,?,?,?,?,?,?,?)',
               (bag_id,version,pick['method'],1 if pick['confirmed'] is not None else 0,
                pick['confirmed'],pick['lo'],pick['hi'],','.join(pick['issues']) or 'none',
                pick['ref'],pick['at']))
    ticket={'bag_id':bag_id,'version':version,'method':pick['method'],'reliable':pick['confirmed'] is not None,
            'confirmed_kg':pick['confirmed'],'feasible_min_kg':pick['lo'],'feasible_max_kg':pick['hi'],
            'issues':pick['issues'],'evidence_ref':pick['ref']}
    if pick['confirmed'] is not None:
        settle(db,bag_id,pick['confirmed'],pick['method'],version,pick['at'])
        from .baggage import publish_acceptance
        seq=db.execute('SELECT COALESCE(MAX(id),0)+1 FROM decision').fetchone()[0]
        publish_acceptance(db,bag_id,pick['confirmed'],seq,pick['ref'])
    return ticket

def ensure_pending(db,bag_id):
    """A bag with force on the scale but no confirmed mass waits in the reweigh queue."""
    db.execute("INSERT INTO acceptance VALUES(?,?,?,?,?,?) ON CONFLICT(bag_id) DO NOTHING",
               (bag_id,'pending','awaiting_confirmed_weighing','none',None,0))

def reconstruct_bag(db,bag_id):
    ticket=_attempt(db,bag_id)
    return [ticket] if ticket else []

def reconstruct_scale(db,scale_id):
    """One shared series settles every unsettled bag sampled on this scale's segment."""
    tickets=[]
    for row in all_rows(db,'SELECT DISTINCT bag_id FROM scale_sample WHERE scale_id=?',(scale_id,)):
        ticket=_attempt(db,row['bag_id'])
        if ticket:tickets.append(ticket)
    return tickets
