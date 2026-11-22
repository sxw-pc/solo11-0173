"""Collect anonymous counts and versioned wait evidence at a reclaim carousel.

The original anonymous door-frame pipeline (``import_tally`` ->
``queue_service.project``) is unchanged: it only ever stores counts and the
raw page bytes.  Voluntary ticket numbers and anonymous track identifiers live
in separate versioned tables created in ``store``; the count tables are never
rewritten into identity records.
"""
import json
from pathlib import Path

from .store import canonical, connect, digest
from .wait_bounds import PROMISE_SECONDS, build_world, compute_bounds, parse_ts


def import_zone(db_path, file):
    doc = json.loads(Path(file).read_text())
    with connect(db_path) as db, db:
        if not doc['zone_id'] or doc['capacity'] <= 0:
            raise ValueError('invalid_zone')
        db.execute('INSERT INTO reclaim_zone VALUES(?,?,?)',
                   (doc['zone_id'], doc['carousel'], doc['capacity']))
    return {'zone_id': doc['zone_id']}


def import_tally(db_path, file):
    raw = Path(file).read_text()
    doc = json.loads(raw)
    with connect(db_path) as db, db:
        if doc['entered'] < 0 or doc['exited'] < 0:
            raise ValueError('invalid_tally')
        if not db.execute('SELECT 1 FROM reclaim_zone WHERE id=?', (doc['zone_id'],)).fetchone():
            raise ValueError('unknown_zone')
        db.execute('INSERT INTO reclaim_tally VALUES(?,?,?,?,?,?)',
                   (doc['tally_id'], doc['zone_id'], doc['at'],
                    doc['entered'], doc['exited'], raw))
        from .queue_service import project
        return project(db, doc['zone_id'])


# --------------------------------------------------------------------------- #
# Versioned wait evidence: gate calibration, tracks, tickets, carousel runs
# --------------------------------------------------------------------------- #
def _load_pages(db, zone_id):
    """Door pages retained as raw bytes; occluded pages may carry null counts."""
    rows = db.execute(
        'SELECT raw FROM reclaim_tally WHERE zone_id=? ORDER BY at,id', (zone_id,)).fetchall()
    pages = []
    for row in rows:
        page = json.loads(row['raw'])
        pages.append({
            'page_id': page['tally_id'],
            'gate_id': page.get('gate_id', 'G1'),
            'at': page['at'],
            'entered': page.get('entered'),
            'exited': page.get('exited'),
            'reset': bool(page.get('reset', False)),
            'occluded': bool(page.get('occluded', False)),
            'capacity_bound': page.get('capacity_bound', 0),
        })
    return pages


def _ingest_bundle_pages(db, zone_id, pages):
    """Persist bundle door pages alongside the anonymous tally originals.

    Ordinary count pages keep going through ``import_tally``; only reset and
    occluded pages (whose counter reading is null or restarted) land here.
    Their raw bytes are retained exactly, and a repeated bundle is idempotent.
    """
    for page in pages or []:
        tally_id = page['tally_id']
        if db.execute('SELECT 1 FROM reclaim_tally WHERE id=?', (tally_id,)).fetchone():
            continue
        entered = page.get('entered')
        exited = page.get('exited')
        if not page.get('occluded'):
            if entered is None or exited is None or entered < 0 or exited < 0:
                raise ValueError('invalid_tally:' + str(tally_id))
        db.execute('INSERT INTO reclaim_tally VALUES(?,?,?,?,?,?)',
                   (tally_id, zone_id, page['at'], entered, exited,
                    canonical(page)))


def _next_version(db, zone_id):
    row = db.execute('SELECT COALESCE(MAX(version),0)+1 AS v FROM reclaim_wait_certificate '
                     'WHERE zone_id=?', (zone_id,)).fetchone()
    return row['v']


def _voucher_decisions(world, result, ticket_sources):
    """Remediation is ticket-by-ticket, with independent evidence at both ends.

    The aggregate upper bound is never distributed onto a person.  Decisions
    use the lower-bound certificate pairing -- one concrete, globally feasible
    assignment whose single shared gate-offset vector applies to every
    passenger in the run -- together with each ticket edge's own best/worst
    waiting interval over the calibration box:

    * ``issued`` only when the edge breaches even under the most favourable
      possible offsets (a sound "necessarily over promise" decision);
    * ``held`` when the edge could breach under some offset but is not forced
      to, so the money cannot be paid without fabricating certainty;
    * ``rejected`` when even the worst-case wait is inside the promise.
    """
    vouchers = []
    rows = {row['ticket_no']: row for row in result['lower_pairing']
            if row['ticket_no']}
    for ticket_no, row in rows.items():
        sources = ticket_sources.get(ticket_no)
        if not sources or not sources.get('entry_sha') or not sources.get('exit_sha'):
            status, reason = 'held', 'ticket_missing_two_end_evidence'
        elif row['wait_seconds_min'] > world['threshold']:
            status, reason = 'issued', 'ticket_pair_certainly_over_promise'
        elif row['wait_seconds_max'] > world['threshold']:
            status, reason = 'held', 'ticket_pair_only_possibly_over_promise'
        else:
            status, reason = 'rejected', 'ticket_pair_within_promise'
        vouchers.append({
            'ticket_no': ticket_no, 'entry_at': row['entry_at'],
            'exit_at': row['exit_at'], 'wait_seconds': row['realised_wait_seconds'],
            'wait_seconds_min': row['wait_seconds_min'],
            'wait_seconds_max': row['wait_seconds_max'],
            'status': status, 'reason': reason,
            'entry_source_sha': sources['entry_sha'] if sources else None,
            'exit_source_sha': sources['exit_sha'] if sources else None,
        })
    return vouchers


def _import_window(db, window, sha, seq):
    """Build one versioned certificate and its ticket vouchers for a zone."""
    zone_id = window['zone_id']
    if not db.execute('SELECT 1 FROM reclaim_zone WHERE id=?', (zone_id,)).fetchone():
        raise ValueError('unknown_zone:' + zone_id)
    version = _next_version(db, zone_id)
    _ingest_bundle_pages(db, zone_id, window.get('pages'))
    # The live head-count control keeps running from the retained counts;
    # evidence pages refresh it exactly as the anonymous tally path would.
    from .queue_service import project
    projection = project(db, zone_id)
    calibration = {g: tuple(span) for g, span in window.get('calibration', {}).items()}
    for gate, span in calibration.items():
        db.execute('INSERT INTO reclaim_gate_calibration VALUES(?,?,?,?,?)',
                   (zone_id, gate, version, int(span[0]), int(span[1])))

    anchors = []
    ticket_sources = {}
    for track in window.get('tracks', []):
        # Tracks carry only an anonymous id; even complete ones never identify
        # a passenger and can never spawn a remediation voucher.
        anchors.append({
            'anchor_id': track['track_id'],
            'entry_gate': track.get('entry_gate', 'G1'),
            'exit_gate': track.get('exit_gate', 'G1'),
            'entry_at': track['entry_at'], 'exit_at': track['exit_at'],
            'co_witness': track.get('co_witness'),
        })
    for ticket in window.get('tickets', []):
        anchors.append({
            'anchor_id': 'TKT-' + ticket['ticket_no'],
            'entry_gate': ticket.get('entry_gate', 'G1'),
            'exit_gate': ticket.get('exit_gate', 'G1'),
            'entry_at': ticket['entry_at'], 'exit_at': ticket['exit_at'],
            'ticket_no': ticket['ticket_no'],
            'co_witness': ticket.get('co_witness'),
        })
        ticket_sources[ticket['ticket_no']] = {
            'entry_sha': ticket.get('entry_source_sha', sha),
            'exit_sha': ticket.get('exit_source_sha', sha),
        }

    pages = _load_pages(db, zone_id)
    world = build_world(pages, calibration=calibration,
                        runs=window.get('carousel_runs'), anchors=anchors,
                        threshold=int(window.get('threshold_seconds', PROMISE_SECONDS)))
    result = compute_bounds(world)

    window_start = min((parse_ts(p['at']) for p in pages), default=None)
    window_end = max((parse_ts(p['at']) for p in pages), default=None)
    basis = canonical({
        'calibration': calibration,
        'runs': window.get('carousel_runs', []),
        'track_ids': [t['track_id'] for t in window.get('tracks', [])],
        'ticket_nos': [t['ticket_no'] for t in window.get('tickets', [])],
        'problems': result['problems'],
    })
    db.execute(
        'INSERT INTO reclaim_wait_certificate(cert_id,zone_id,version,window_start,'
        'window_end,threshold_seconds,lower_bound,upper_bound,observed_entered,'
        'observed_exited,status,problems,lower_pairing,upper_pairing,basis,sources,created_seq) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (window['cert_id'], zone_id, version,
         _iso_or_none(window_start), _iso_or_none(window_end),
         result['threshold_seconds'], result['lower_bound'], result['upper_bound'],
         result['observed_entered'], result['observed_exited'], result['status'],
         json.dumps(result['problems']),
         canonical(result['lower_pairing']), canonical(result['upper_pairing']),
         basis, json.dumps([sha]), seq))

    vouchers = _voucher_decisions(world, result, ticket_sources)
    for i, v in enumerate(vouchers, start=1):
        db.execute(
            'INSERT INTO reclaim_ticket_voucher(voucher_id,cert_id,zone_id,ticket_no,'
            'entry_at,exit_at,wait_seconds,status,reason,entry_source_sha,'
            'exit_source_sha,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('%s-V%d-%d' % (window['cert_id'], version, i), window['cert_id'], zone_id,
             v['ticket_no'], v['entry_at'], v['exit_at'],
             int(v['wait_seconds']), v['status'], v['reason'],
             v['entry_source_sha'], v['exit_source_sha'], version))

    return {
        'zone_id': zone_id, 'cert_id': window['cert_id'], 'version': version,
        'status': result['status'], 'problems': result['problems'],
        'lower_bound': result['lower_bound'], 'upper_bound': result['upper_bound'],
        'queue_count': projection['queue_count'], 'queue_status': projection['status'],
        'vouchers': vouchers,
    }


def import_wait_bundle(db_path, file):
    """Import one approved evidence bundle and issue versioned certificates.

    A bundle holds one or more service windows.  Each window provides its
    ``zone_id``, ``cert_id``, optional ``calibration``
    (gate -> [lo_seconds, hi_seconds]), ``carousel_runs``, ``tracks``
    (anonymous continuous top-view trajectories) and ``tickets``
    (voluntary pickup receipts).  Door pages are the retained
    ``reclaim_tally`` originals plus any reset/occluded pages carried in the
    bundle; identity evidence never re-derives a count.
    """
    raw = Path(file).read_bytes()
    doc = json.loads(raw)
    sha = digest(raw)
    windows = doc['windows'] if 'windows' in doc else [doc]
    primary_zone = windows[0]['zone_id']
    with connect(db_path) as db, db:
        if db.execute('SELECT 1 FROM reclaim_evidence_file WHERE sha=?', (sha,)).fetchone():
            return {'status': 'replayed', 'source_sha': sha,
                    'zones': [w['zone_id'] for w in windows]}
        db.execute(
            'INSERT INTO reclaim_evidence_file(sha,zone_id,kind,raw,imported_seq) '
            'VALUES(?,?,?,?,?)',
            (sha, primary_zone, 'wait_bundle', raw,
             db.execute('SELECT COALESCE(MAX(imported_seq),0)+1 FROM reclaim_evidence_file')
              .fetchone()[0]))
        seq = db.execute('SELECT COALESCE(MAX(created_seq),0)+1 '
                         'FROM reclaim_wait_certificate').fetchone()[0]
        results = []
        for window in windows:
            results.append(_import_window(db, window, sha, seq))
            seq += 1
        return {'status': 'ok', 'source_sha': sha, 'results': results}


def _iso_or_none(epoch):
    if epoch is None:
        return None
    from .wait_bounds import iso
    return iso(epoch)


def get_certificate(db_path, zone_id, version=None):
    """Return one versioned wait certificate, or the latest when unspecified."""
    with connect(db_path) as db:
        if version is None:
            row = db.execute('SELECT * FROM reclaim_wait_certificate WHERE zone_id=? '
                             'ORDER BY version DESC LIMIT 1', (zone_id,)).fetchone()
        else:
            row = db.execute('SELECT * FROM reclaim_wait_certificate WHERE zone_id=? AND version=?',
                             (zone_id, version)).fetchone()
        if row is None:
            raise ValueError('unknown_certificate')
        return {
            'cert_id': row['cert_id'], 'zone_id': row['zone_id'],
            'version': row['version'], 'window_start': row['window_start'],
            'window_end': row['window_end'], 'threshold_seconds': row['threshold_seconds'],
            'lower_bound': row['lower_bound'], 'upper_bound': row['upper_bound'],
            'observed_entered': row['observed_entered'],
            'observed_exited': row['observed_exited'], 'status': row['status'],
            'problems': json.loads(row['problems']),
            'lower_pairing': json.loads(row['lower_pairing']),
            'upper_pairing': json.loads(row['upper_pairing']),
            'basis': json.loads(row['basis']), 'sources': json.loads(row['sources']),
        }


def get_vouchers(db_path, zone_id, version=None):
    """Return ticket remediation vouchers, newest version first by default."""
    with connect(db_path) as db:
        if version is None:
            rows = db.execute('SELECT * FROM reclaim_ticket_voucher WHERE zone_id=? '
                              'ORDER BY version DESC,voucher_id', (zone_id,)).fetchall()
        else:
            rows = db.execute('SELECT * FROM reclaim_ticket_voucher WHERE zone_id=? AND version=? '
                              'ORDER BY voucher_id', (zone_id, version)).fetchall()
        return [dict(r) for r in rows]
