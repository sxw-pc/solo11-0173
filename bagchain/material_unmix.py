"""Bounded multi-channel material unmixing with a shared device calibration.

The engine never picks a single zero-residual decomposition when several are
compatible with the same three-channel reading.  Every voxel keeps the full set
of feasible compositions, and the bag-level restricted-material fraction is an
upper/lower interval computed with the calibration bias shared across *all*
voxels of one batch.  A per-voxel bias choice would silently inflate the bag
interval, so the joint linear program uses one bias vector for the batch.

The linear programs use a standard-library two-phase tableau simplex with a
feasibility gate; when SciPy is available it independently cross-checks any
result the tableau cannot certify.  Nominal residuals use a simplex fit with a
projected-gradient fallback for readings outside the reference simplex.
"""
import json
import math

from .store import all_rows, one, record

EPS = 1e-9
FRACTION_EPS = 1e-8


# Linear programs live in :mod:`bagchain.lp`: a standard-library tableau simplex
# with a feasibility gate and an optional SciPy cross-check.
from .lp import linprog, _violation


# --------------------------------------------------------------------------
# Reference geometry: R, P, C with C = (R + P) / 2 creates a one dimensional
# null direction inside the mixture simplex (R + P - 2C = 0, coefficients sum 0).
# --------------------------------------------------------------------------
def null_direction(refs):
    n = len(refs)
    m = len(refs[0])
    matrix = [[refs[i][j] for i in range(n)] for j in range(m)] + [[1.0] * n]
    pivot_cols = []
    row = 0
    for col in range(n):
        pivot = max(range(row, len(matrix)), key=lambda r: abs(matrix[r][col]))
        if abs(matrix[pivot][col]) < 1e-10:
            continue
        matrix[row], matrix[pivot] = matrix[pivot], matrix[row]
        pivot_value = matrix[row][col]
        matrix[row] = [v / pivot_value for v in matrix[row]]
        for r in range(len(matrix)):
            if r != row:
                factor = matrix[r][col]
                matrix[r] = [matrix[r][k] - factor * matrix[row][k] for k in range(n)]
        pivot_cols.append(col)
        row += 1
    free = [c for c in range(n) if c not in pivot_cols]
    if not free:
        return None
    direction = [0.0] * n
    fc = free[0]
    direction[fc] = 1.0
    for i, pc in enumerate(pivot_cols):
        direction[pc] = -matrix[i][fc]
    return direction


def best_simplex_fit(reading, refs):
    """Nearest point on the unit mixture simplex.

    Variables are (s_1, ..., s_n, e) with the reading reconstructed by the
    reference mixture and e the maximum per-channel deviation (an L-infinity
    residual solved with one LP).  When the reading lies on the simplex the
    residual is zero and several weight sets may tie; callers must never treat
    the returned weights as unique evidence of composition.
    """
    n = len(refs)
    m = len(reading)
    A = []
    b = []
    for j in range(m):
        A.append([refs[i][j] for i in range(n)] + [1.0])
        b.append(reading[j])
        A.append([-refs[i][j] for i in range(n)] + [1.0])
        b.append(-reading[j])
    A.append([1.0] * n + [0.0])
    b.append(1.0)
    A.append([-1.0] * n + [0.0])
    b.append(-1.0)
    # The residual magnitude e is itself non-negative.
    A.append([0.0] * n + [-1.0])
    b.append(0.0)
    x = linprog([0.0] * n + [1.0], A, b)
    if x is None:
        # No bounded residual exists within the LP tolerance; report the
        # Euclidean distance to the closest simplex point instead of failing.
        residual, weights = _euclidean_simplex_fit(reading, refs)
        return residual, weights
    weights = [max(0.0, v) for v in x[:n]]
    total = sum(weights)
    if total > EPS:
        weights = [w / total for w in weights]
    return x[-1], weights


def _euclidean_simplex_fit(reading, refs):
    """Closest point on the unit simplex by projected-gradient (no LP needed)."""
    n = len(refs)
    # Project onto the simplex in coefficient space using the normal-equations
    # projection onto the affine hull followed by a sum projection.
    weights = [1.0 / n] * n
    step = 0.1
    for _ in range(20000):
        grad = [0.0] * n
        for j in range(len(reading)):
            pred = sum(weights[i] * refs[i][j] for i in range(n))
            delta = pred - reading[j]
            for i in range(n):
                grad[i] += 2.0 * delta * refs[i][j]
        candidate = [max(0.0, weights[i] - step * grad[i]) for i in range(n)]
        total = sum(candidate)
        if total > EPS:
            candidate = [w / total for w in candidate]
        weights = candidate
    residual = math.sqrt(sum(
        (reading[j] - sum(weights[i] * refs[i][j] for i in range(n))) ** 2
        for j in range(len(reading))))
    return residual, weights


def nominal_residual(reading, refs):
    """L-infinity residual of the best simplex fit at nominal calibration."""
    residual, _ = best_simplex_fit(reading, refs)
    return residual


# --------------------------------------------------------------------------
# Per-voxel primal LP.  Variables (s, u, q) = thickness * weights so that the
# reading is s*R + u*P + q*C.  The thickness interval bounds t = s+u+q and the
# calibration bias box widens every channel strip.  Feasibility is exact.
# --------------------------------------------------------------------------
def _voxel_strips(reading, refs, bias_limit, t_lo, t_hi):
    A = []
    b = []
    for j in range(len(reading)):
        A.append([refs[i][j] for i in range(len(refs))])
        b.append(reading[j] + bias_limit)
        A.append([-refs[i][j] for i in range(len(refs))])
        b.append(bias_limit - reading[j])
    A.append([1.0] * len(refs))
    b.append(t_hi)
    A.append([-1.0] * len(refs))
    b.append(-t_lo)
    return A, b


def voxel_fraction_bounds(reading, refs, bias_limit, t_lo, t_hi, restricted_index=0):
    A, b = _voxel_strips(reading, refs, bias_limit, t_lo, t_hi)
    n = len(refs)
    low_x = linprog([1.0 if i == restricted_index else 0.0 for i in range(n)], A, b)
    high_x = linprog([-1.0 if i == restricted_index else 0.0 for i in range(n)], A, b)
    if low_x is None or high_x is None:
        return None
    if _violation(low_x, A, b) > 1e-7 or _violation(high_x, A, b) > 1e-7:
        return None
    t_low = sum(low_x)
    t_high = sum(high_x)
    if t_low <= EPS or t_high <= EPS:
        return None
    return low_x[restricted_index] / t_low, high_x[restricted_index] / t_high


# --------------------------------------------------------------------------
# Joint bag LP with ONE bias vector shared by every voxel.  Variables per
# voxel (s, u, q) plus the shared bias split (b+_j, b-_j >= 0, b_j = b+_j - b-_j,
# each bounded by the calibration limit).  A sign-free bias cannot be an LP
# variable directly, so the split keeps every variable non-negative.  A
# sum-of-ratios objective is solved with a generalized Dinkelbach iteration.
# --------------------------------------------------------------------------
def _joint_program(voxels, refs, bias_limit, t_lo_fn, t_hi_fn):
    n_refs = len(refs)
    n_v = len(voxels)
    block = n_refs * n_v
    bias_plus = block
    bias_minus = block + n_refs
    n_vars = block + 2 * n_refs
    A = []
    b = []
    for v, voxel in enumerate(voxels):
        base = v * n_refs
        t_lo, t_hi = t_lo_fn(voxel), t_hi_fn(voxel)
        for j in range(n_refs):
            row = [0.0] * n_vars
            for i, ref in enumerate(refs):
                row[base + i] = ref[j]
            row[bias_plus + j] = 1.0
            row[bias_minus + j] = -1.0
            A.append(row)
            b.append(voxel['channels'][j] + bias_limit)
            row = [0.0] * n_vars
            for i, ref in enumerate(refs):
                row[base + i] = -ref[j]
            row[bias_plus + j] = -1.0
            row[bias_minus + j] = 1.0
            A.append(row)
            b.append(bias_limit - voxel['channels'][j])
        row = [0.0] * n_vars
        for i in range(n_refs):
            row[base + i] = 1.0
        A.append(row)
        b.append(t_hi)
        row = [0.0] * n_vars
        for i in range(n_refs):
            row[base + i] = -1.0
        A.append(row)
        b.append(-t_lo)
    for j in range(n_refs):
        row = [0.0] * n_vars
        row[bias_plus + j] = 1.0
        A.append(row)
        b.append(bias_limit)
        row = [0.0] * n_vars
        row[bias_minus + j] = 1.0
        A.append(row)
        b.append(bias_limit)
    return A, b, n_vars


def bag_fraction_bounds(voxels, refs, bias_limit, thickness_relative, volumes,
                        restricted_index=0, unexplained_ids=()):
    """Shared-calibration bag interval; occluded and unexplained voxels span [0, 1]."""
    n_refs = len(refs)
    unexplained = set(unexplained_ids)

    def t_lo(voxel):
        return voxel['thickness'] * (1.0 - thickness_relative)

    def t_hi(voxel):
        return voxel['thickness'] * (1.0 + thickness_relative)

    def unevidenced(voxel):
        return voxel['occluded'] or voxel.get('voxel_id') in unexplained

    clear = [v for v in voxels if not unevidenced(v)]
    unknown = [v for v in voxels if unevidenced(v)]
    total_volume = sum(volumes)
    unknown_volume = sum(v['volume'] for v in unknown)
    clear_volume = total_volume - unknown_volume

    if not clear:
        return 0.0, 1.0, unknown_volume, 0.0

    A, b, n_vars = _joint_program(clear, refs, bias_limit, t_lo, t_hi)
    n_v = len(clear)
    weights = [v['volume'] / clear_volume for v in clear]

    def optimize(sign):
        lambdas = [0.25] * n_v
        x = None
        for _ in range(2000):
            cost = [0.0] * n_vars
            for v in range(n_v):
                base = v * n_refs
                for i in range(n_refs):
                    cost[base + i] = sign * (
                        -weights[v] if i == restricted_index else 0.0
                    ) + sign * lambdas[v] * weights[v]
            x = linprog(cost, A, b)
            if x is None:
                raise ValueError('joint_lp_infeasible')
            updated = [x[v * n_refs + restricted_index]
                       / sum(x[v * n_refs:v * n_refs + n_refs]) for v in range(n_v)]
            if max(abs(p - q) for p, q in zip(lambdas, updated)) < 1e-11:
                lambdas = updated
                break
            lambdas = updated
        return sum(weights[v] * lambdas[v] for v in range(n_v))

    clear_lo = optimize(-1)
    clear_hi = optimize(1)
    # Voxels without usable evidence (metal shadow or unexplained residual)
    # carry no fraction: their full volume can be restricted or free.
    bag_lo = clear_lo * clear_volume / total_volume
    bag_hi = (clear_hi * clear_volume + unknown_volume) / total_volume
    return bag_lo, bag_hi, unknown_volume, clear_volume


def _independent_voxel_bounds(reading, refs, bias_limit, t_lo, t_hi,
                              restricted_index=0):
    """Per-voxel LP with its own bias vector (the deliberately weak baseline).

    Variables are the non-negative mixture scales (s_i) and a sign split of the
    private bias (b+_j, b-_j), so the tableau stays non-negative throughout.
    """
    n = len(refs)
    width = n + 2 * n
    A = []
    b = []
    for j in range(n):
        row = [refs[i][j] for i in range(n)]
        row += [1.0 if k == j else 0.0 for k in range(n)]
        row += [-1.0 if k == j else 0.0 for k in range(n)]
        A.append(row)
        b.append(reading[j] + bias_limit)
        row = [-refs[i][j] for i in range(n)]
        row += [-1.0 if k == j else 0.0 for k in range(n)]
        row += [1.0 if k == j else 0.0 for k in range(n)]
        A.append(row)
        b.append(bias_limit - reading[j])
    A.append([1.0] * n + [0.0] * 2 * n)
    b.append(t_hi)
    A.append([-1.0] * n + [0.0] * 2 * n)
    b.append(-t_lo)
    for j in range(n):
        row = [0.0] * n + [1.0 if k == j else 0.0 for k in range(n)] + [0.0] * n
        A.append(row)
        b.append(bias_limit)
        row = [0.0] * n + [0.0] * n + [1.0 if k == j else 0.0 for k in range(n)]
        A.append(row)
        b.append(bias_limit)
    low_cost = [1.0 if i == restricted_index else 0.0 for i in range(n)] + [0.0] * 2 * n
    high_cost = [-1.0 if i == restricted_index else 0.0 for i in range(n)] + [0.0] * 2 * n
    low_x = linprog(low_cost, A, b)
    high_x = linprog(high_cost, A, b)
    if low_x is None or high_x is None:
        return None
    if _violation(low_x, A, b) > 1e-7 or _violation(high_x, A, b) > 1e-7:
        return None
    t_low = sum(low_x[:n])
    t_high = sum(high_x[:n])
    if t_low <= EPS or t_high <= EPS:
        return None
    return low_x[restricted_index] / t_low, high_x[restricted_index] / t_high


def independent_bag_bounds(voxels, refs, bias_limit, thickness_relative,
                           restricted_index=0):
    """Reference calculation that lets every voxel pick its own bias.

    Each voxel optimizes over a private calibration vector, so the bag interval
    is the volume-weighted sum of independent worst cases.  This is deliberately
    weaker than :func:`bag_fraction_bounds`, whose single shared bias vector is
    chosen once for the whole batch; tests use it to prove the shared interval
    is strictly tighter.
    """
    total = sum(v['volume'] for v in voxels)
    low = high = 0.0
    for voxel in voxels:
        if voxel['occluded']:
            high += voxel['volume']
            continue
        t0 = voxel['thickness']
        rng = _independent_voxel_bounds(
            voxel['channels'], refs, bias_limit,
            t0 * (1.0 - thickness_relative), t0 * (1.0 + thickness_relative),
            restricted_index)
        if rng is None:
            high += voxel['volume']
            continue
        low += rng[0] * voxel['volume']
        high += rng[1] * voxel['volume']
    return low / total, high / total


# --------------------------------------------------------------------------
# Fourth energy channel: a full-rank fourth channel makes the mixture unique
# for voxels that actually have a fourth-channel reading.
# --------------------------------------------------------------------------
def fourth_channel_weights(channels3, channel4, refs3, refs4):
    # Design matrix: one row per energy channel, one column per material.
    n = len(refs3)
    design = [[row[j] for row in refs3] for j in range(n)]
    design.append([refs4[i] for i in range(n)])
    target = list(channels3) + [channel4]
    channels = n + 1
    gram = [[sum(design[j][a] * design[j][b] for j in range(channels))
             for b in range(n)] for a in range(n)]
    rhs = [sum(design[j][a] * target[j] for j in range(channels)) for a in range(n)]
    matrix = [gram[i][:] + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(matrix[r][col]))
        if abs(matrix[pivot][col]) < 1e-12:
            raise ValueError('fourth_channel_rank_deficient')
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        pivot_value = matrix[col][col]
        matrix[col] = [v / pivot_value for v in matrix[col]]
        for r in range(n):
            if r != col:
                factor = matrix[r][col]
                matrix[r] = [matrix[r][c] - factor * matrix[col][c] for c in range(n + 1)]
    weights = [matrix[i][n] / matrix[i][i] for i in range(n)]
    residual = math.sqrt(sum(
        (target[j] - sum(weights[i] * design[j][i] for i in range(n))) ** 2
        for j in range(channels)))
    return weights, residual


# --------------------------------------------------------------------------
# Event handlers
# --------------------------------------------------------------------------
def _load_refs(db, bag, revision):
    rows = all_rows(db,
        'SELECT material,channels_json FROM screen_profile WHERE bag_id=? AND revision=? ORDER BY profile_id',
        (bag, revision))
    if not rows:
        raise ValueError('missing_reference_revision')
    return [(r['material'], json.loads(r['channels_json'])) for r in rows]


def _validate_calibration(e):
    calibration = e.get('calibration')
    if not isinstance(calibration, dict):
        raise ValueError('calibration_required')
    bias = calibration.get('bias_limit')
    relative = calibration.get('thickness_relative')
    if not isinstance(bias, (int, float)) or bias < 0:
        raise ValueError('invalid_bias_limit')
    if not isinstance(relative, (int, float)) or not 0 <= relative < 1:
        raise ValueError('invalid_thickness_relative')
    for key in ('calibration_id', 'calibrated_at'):
        if not calibration.get(key):
            raise ValueError('calibration_identity_required')
    return calibration


def _validate_voxel(voxel, refs):
    if not voxel.get('voxel_id') or not voxel.get('position'):
        raise ValueError('voxel_identity_required')
    channels = voxel.get('channels')
    if not isinstance(channels, list) or len(channels) != len(refs[0][1]):
        raise ValueError('voxel_channel_count')
    if any(not isinstance(x, (int, float)) or x < 0 for x in channels):
        raise ValueError('invalid_voxel_channels')
    if not isinstance(voxel.get('thickness'), (int, float)) or voxel['thickness'] <= 0:
        raise ValueError('invalid_thickness')
    if not isinstance(voxel.get('volume'), (int, float)) or voxel['volume'] <= 0:
        raise ValueError('invalid_voxel_volume')
    if not isinstance(voxel.get('occluded'), bool):
        raise ValueError('occluded_flag_required')
    if not isinstance(voxel.get('inspectable'), bool):
        raise ValueError('inspectable_flag_required')


def batch(db, e, seq):
    if not one(db, 'SELECT 1 FROM bag_tag WHERE bag_id=?', (e['bag_id'],)):
        raise ValueError('unknown_bag')
    scan = one(db, 'SELECT 1 FROM screen_scan WHERE scan_id=? AND bag_id=?',
               (e['scan_id'], e['bag_id']))
    if not scan:
        raise ValueError('unknown_screen_scan')
    if one(db, 'SELECT 1 FROM unmix_batch WHERE batch_id=?', (e['batch_id'],)):
        raise ValueError('batch_id_conflict')
    refs = _load_refs(db, e['bag_id'], e['reference_revision'])
    names = [name for name, _ in refs]
    restricted = e.get('restricted_material')
    if restricted not in names:
        raise ValueError('restricted_material_unknown')
    restricted_index = names.index(restricted)
    ref_vectors = [vector for _, vector in refs]
    calibration = _validate_calibration(e)
    voxels = e.get('voxels')
    if not isinstance(voxels, list) or not voxels:
        raise ValueError('voxels_required')
    for voxel in voxels:
        _validate_voxel(voxel, refs)
    if len({v['voxel_id'] for v in voxels}) != len(voxels):
        raise ValueError('duplicate_voxel_id')
    bias_limit = float(calibration['bias_limit'])
    relative = float(calibration['thickness_relative'])
    total_volume = sum(float(v['volume']) for v in voxels)

    db.execute(
        'INSERT INTO unmix_batch VALUES(?,?,?,?,?,?,?,?)',
        (e['batch_id'], e['bag_id'], e['scan_id'], e['reference_revision'],
         json.dumps(calibration, sort_keys=True), total_volume, 0.0, seq))

    unexplained_volume = 0.0
    unexplained_ids = set()
    rescan_positions = []
    for voxel in voxels:
        channels = [float(x) for x in voxel['channels']]
        residual = nominal_residual(channels, ref_vectors)
        if voxel['occluded']:
            rng = None
            state = 'occluded_pending'
            rescan_positions.append(voxel['position'])
        else:
            t0 = float(voxel['thickness'])
            rng = voxel_fraction_bounds(
                channels, ref_vectors, bias_limit,
                t0 * (1.0 - relative), t0 * (1.0 + relative), restricted_index)
            if rng is None:
                state = 'unexplained_pending'
                unexplained_volume += float(voxel['volume'])
                unexplained_ids.add(voxel['voxel_id'])
                rescan_positions.append(voxel['position'])
            else:
                state = 'bounded'
        db.execute(
            'INSERT INTO unmix_voxel VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (voxel['voxel_id'], e['batch_id'], e['bag_id'],
             json.dumps(channels), json.dumps(float(voxel['thickness'])),
             1 if voxel['occluded'] else 0, float(voxel['volume']),
             1 if voxel['inspectable'] else 0, voxel['position'],
             None if rng is None else rng[0], None if rng is None else rng[1],
             residual, state))

    bag_lo, bag_hi, unknown_volume, clear_volume = bag_fraction_bounds(
        [{**v, 'channels': [float(x) for x in v['channels']],
          'thickness': float(v['thickness'])} for v in voxels],
        ref_vectors, bias_limit, relative,
        [float(v['volume']) for v in voxels], restricted_index,
        unexplained_ids=unexplained_ids)
    db.execute('UPDATE unmix_batch SET occluded_volume=? WHERE batch_id=?',
               (unknown_volume, e['batch_id']))
    db.execute(
        'INSERT INTO unmix_bag_result VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (e['batch_id'] + ':R1', e['batch_id'], e['bag_id'], 'three_channel', 1,
         bag_lo, bag_hi, clear_volume, total_volume, unknown_volume,
         unexplained_volume, json.dumps(sorted(set(rescan_positions))),
         'pending_review' if rescan_positions else 'bounded'))
    record(db, e['bag_id'], 'material_unmix', e['event_id'],
           'bounded', 'three_channel_unmix', seq)
    return {'status': 'bounded', 'reason': 'three_channel_unmix',
            'r_fraction_range': [bag_lo, bag_hi],
            'rescan_positions': sorted(set(rescan_positions))}


def _current_result(db, batch_id):
    return one(db,
        'SELECT * FROM unmix_bag_result WHERE batch_id=? ORDER BY result_version DESC LIMIT 1',
        (batch_id,))


def rescan(db, e, seq):
    """Route A: qualified fourth-energy-channel rescan, version pinned."""
    batch_row = one(db, 'SELECT * FROM unmix_batch WHERE batch_id=?', (e['batch_id'],))
    if not batch_row or batch_row['bag_id'] != e['bag_id']:
        raise ValueError('unknown_unmix_batch')
    if one(db, 'SELECT 1 FROM unmix_followup WHERE followup_id=?', (e['followup_id'],)):
        raise ValueError('followup_id_conflict')
    refs = _load_refs(db, e['bag_id'], batch_row['reference_revision'])
    names = [name for name, _ in refs]
    refs3 = [vector for _, vector in refs]
    refs4 = e.get('reference_channel4')
    if not isinstance(refs4, dict) or set(refs4) != set(names):
        raise ValueError('reference_channel4_must_pin_all_materials')
    if not e.get('device_id') or not e.get('calibration_version'):
        raise ValueError('device_calibration_identity_required')
    readings = e.get('voxel_channel4')
    if not isinstance(readings, dict) or not readings:
        raise ValueError('fourth_channel_readings_required')
    calibration = json.loads(batch_row['calibration_json'])
    restricted = e.get('restricted_material')
    if restricted not in names:
        raise ValueError('restricted_material_unknown')
    r_index = names.index(restricted)
    ref4 = [float(refs4[name]) for name in names]

    voxels = all_rows(db, 'SELECT * FROM unmix_voxel WHERE batch_id=? ORDER BY voxel_id',
                      (e['batch_id'],))
    evidence = {}
    confirmed_volume = 0.0
    rescan_positions = []
    for voxel in voxels:
        vid = voxel['voxel_id']
        if voxel['occluded'] or vid not in readings or readings[vid] is None:
            evidence[vid] = {'state': 'occluded_pending'}
            rescan_positions.append(voxel['position'])
            continue
        channel4 = float(readings[vid])
        weights, residual = fourth_channel_weights(
            json.loads(voxel['channels_json']), channel4, refs3, ref4)
        if min(weights) < -1e-7 or residual > float(e.get('residual_tolerance', 1e-6)):
            evidence[vid] = {'state': 'inconsistent_pending', 'residual': residual}
            rescan_positions.append(voxel['position'])
            continue
        weights = [max(0.0, w) for w in weights]
        evidence[vid] = {
            'state': 'unique',
            'weights': dict(zip(names, weights)),
            'r_fraction': weights[r_index],
            'residual': residual,
        }
        confirmed_volume += voxel['volume']
        # The original three-channel voxel row is immutable evidence; the
        # fourth-channel decomposition lives only in the followup evidence.

    total = batch_row['total_volume']
    occluded = batch_row['occluded_volume']
    # Bag interval: uniquely decomposed clear volume plus fully unknown voxels.
    r_clear = sum(
        evidence[v['voxel_id']].get('r_fraction', 0.0) * v['volume']
        for v in voxels if evidence[v['voxel_id']]['state'] == 'unique')
    known_volume = sum(
        v['volume'] for v in voxels if evidence[v['voxel_id']]['state'] == 'unique')
    unknown_volume = total - known_volume
    bag_lo = r_clear / total
    bag_hi = (r_clear + unknown_volume) / total
    prior = _current_result(db, e['batch_id'])
    version = prior['result_version'] + 1
    db.execute(
        'INSERT INTO unmix_followup VALUES(?,?,?,?,?,?,?,?,?)',
        (e['followup_id'], e['batch_id'], e['bag_id'], 'A_fourth_channel',
         e['device_id'], e['calibration_version'],
         json.dumps({'readings': readings, 'evidence': evidence,
                     'calibration_at_capture': calibration}, sort_keys=True),
         json.dumps({'confirmed_volume': confirmed_volume}, sort_keys=True), seq))
    db.execute(
        'INSERT INTO unmix_bag_result VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (e['batch_id'] + ':R' + str(version), e['batch_id'], e['bag_id'],
         'A_fourth_channel', version, bag_lo, bag_hi, confirmed_volume, total,
         occluded, prior['unexplained_volume'],
         json.dumps(sorted(set(rescan_positions))),
         'pending_review' if rescan_positions else 'bounded'))
    record(db, e['bag_id'], 'material_unmix', e['event_id'],
           'bounded', 'route_a_fourth_channel', seq)
    return {'status': 'bounded', 'reason': 'route_a_fourth_channel',
            'r_fraction_range': [bag_lo, bag_hi],
            'confirmed_volume': confirmed_volume,
            'rescan_positions': sorted(set(rescan_positions))}


def swab(db, e, seq):
    """Route B: sealed outer-pocket swab, dual-recorded, no inward extrapolation."""
    batch_row = one(db, 'SELECT * FROM unmix_batch WHERE batch_id=?', (e['batch_id'],))
    if not batch_row or batch_row['bag_id'] != e['bag_id']:
        raise ValueError('unknown_unmix_batch')
    if one(db, 'SELECT 1 FROM unmix_followup WHERE followup_id=?', (e['followup_id'],)):
        raise ValueError('followup_id_conflict')
    if not e.get('pocket_position'):
        raise ValueError('pocket_position_required')
    actors = e.get('actors')
    if not isinstance(actors, list) or len(actors) < 2 or len(set(actors)) < 2:
        raise ValueError('dual_record_required')
    if not e.get('passenger_present'):
        raise ValueError('passenger_presence_required')
    if not e.get('seal_id'):
        raise ValueError('sealed_swab_required')
    detected = bool(e.get('r_detected'))
    pocket_voxels = all_rows(db,
        'SELECT * FROM unmix_voxel WHERE batch_id=? AND position=?',
        (e['batch_id'], e['pocket_position']))
    if not pocket_voxels:
        raise ValueError('swab_position_unknown')
    if any(v['occluded'] for v in pocket_voxels):
        raise ValueError('swab_cannot_target_occluded_voxel')
    pocket_volume = sum(v['volume'] for v in pocket_voxels)
    manual_positions = [e['pocket_position']] if detected else []
    # The swab finding is recorded in the followup evidence; original voxel
    # rows stay immutable and no inner-voxel fraction is inferred.
    prior = _current_result(db, e['batch_id'])
    version = prior['result_version'] + 1
    evidence = {
        'seal_id': e['seal_id'],
        'actors': actors,
        'passenger_present': True,
        'r_detected': detected,
        'pocket_position': e['pocket_position'],
        'measured_r_share': e.get('measured_r_share'),
        'note': 'outer_pocket_only_no_inner_extrapolation',
    }
    db.execute(
        'INSERT INTO unmix_followup VALUES(?,?,?,?,?,?,?,?,?)',
        (e['followup_id'], e['batch_id'], e['bag_id'], 'B_pocket_swab',
         e.get('device_id', 'swab-kit'), e.get('calibration_version', 'n/a'),
         json.dumps(evidence, sort_keys=True),
         json.dumps({'confirmed_volume': pocket_volume}, sort_keys=True), seq))
    # The swab cannot change any inner fraction: republish the prior interval
    # verbatim with a new, separately retained version.
    db.execute(
        'INSERT INTO unmix_bag_result VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (e['batch_id'] + ':R' + str(version), e['batch_id'], e['bag_id'],
         'B_pocket_swab', version, prior['r_fraction_lo'], prior['r_fraction_hi'],
         pocket_volume, prior['total_volume'], prior['occluded_volume'],
         prior['unexplained_volume'],
         json.dumps(sorted(set(json.loads(prior['rescan_positions_json'])
                               + manual_positions))),
         'manual_review' if detected else prior['state']))
    record(db, e['bag_id'], 'material_unmix', e['event_id'],
           'manual_review' if detected else 'bounded',
           'route_b_pocket_swab', seq)
    return {'status': 'manual_review' if detected else 'bounded',
            'reason': 'route_b_pocket_swab',
            'r_fraction_range': [prior['r_fraction_lo'], prior['r_fraction_hi']],
            'manual_open_positions': manual_positions}


def manual_open(db, e, seq):
    """Physically opened bag: findings retained next to every prior version."""
    batch_row = one(db, 'SELECT * FROM unmix_batch WHERE batch_id=?', (e['batch_id'],))
    if not batch_row or batch_row['bag_id'] != e['bag_id']:
        raise ValueError('unknown_unmix_batch')
    if one(db, 'SELECT 1 FROM manual_open WHERE open_id=?', (e['open_id'],)):
        raise ValueError('open_id_conflict')
    actors = e.get('actors')
    if not isinstance(actors, list) or len(actors) < 2 or len(set(actors)) < 2:
        raise ValueError('dual_record_required')
    findings = e.get('findings')
    if not isinstance(findings, list) or not findings:
        raise ValueError('findings_required')
    for finding in findings:
        if not finding.get('position') or 'r_volume' not in finding or 'volume' not in finding:
            raise ValueError('finding_identity_required')
        if finding['r_volume'] < 0 or finding['volume'] <= 0 \
                or finding['r_volume'] > finding['volume'] + 1e-9:
            raise ValueError('finding_volume_invalid')
    db.execute(
        'INSERT INTO manual_open VALUES(?,?,?,?,?,?,?)',
        (e['open_id'], e['batch_id'], e['bag_id'], e['at'],
         json.dumps(actors, sort_keys=True), json.dumps(findings, sort_keys=True), seq))
    total = sum(float(f['volume']) for f in findings)
    r_volume = sum(float(f['r_volume']) for f in findings)
    prior = _current_result(db, e['batch_id'])
    version = prior['result_version'] + 1
    measured = r_volume / total
    db.execute(
        'INSERT INTO unmix_bag_result VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (e['batch_id'] + ':R' + str(version), e['batch_id'], e['bag_id'],
         'manual_open', version, measured, measured, total,
         prior['total_volume'], prior['occluded_volume'],
         prior['unexplained_volume'], json.dumps([]),
         'opened_measured'))
    record(db, e['bag_id'], 'material_unmix', e['event_id'],
           'opened_measured', 'manual_open', seq)
    return {'status': 'opened_measured', 'reason': 'manual_open',
            'r_fraction_range': [measured, measured]}
