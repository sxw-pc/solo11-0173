"""Global feasible entry/exit pairings and sound waiting-time bounds.

Door-frame pages only prove *how many* people are inside the reclaim zone at a
moment; they never say which exiting person matches which entering person.
A single greedy pairing (for example FIFO) is therefore not a fact: the same
three pages admit pairings with zero breaches and pairings with one breach.

This module never invents an individual waiting time.  It builds every
anonymised person slot that the evidence requires, then evaluates the *set* of
all globally feasible bipartite pairings and reports:

* ``lower_bound`` -- people that *must* have waited longer than the promise in
  every feasible world (an edge is counted only when it breaches even under the
  most favourable clock bias inside the gate's calibration);
* ``upper_bound`` -- people that *could possibly* have breached (an edge is
  counted when it breaches under some bias inside the calibration);
* a complete, concrete, feasible pairing realising each extreme.

Clock offsets are a property of a *gate* for the whole run: one offset per gate
is shared by every person crossing that gate.  A single shared offset vector is
solved once per presented pairing instead of adjusting times person by person.
Counter resets and occluded pages widen the same flow with anonymous
unobserved slots rather than fabricating observations.
"""
from collections import deque

PROMISE_SECONDS = 15 * 60
EPS = 1e-6


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def parse_ts(value):
    """Parse an ISO-8601 timestamp (trailing Z allowed) to epoch seconds."""
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    from datetime import datetime
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise ValueError('invalid_timestamp:' + str(value)) from None


def iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# --------------------------------------------------------------------------- #
# Evidence normalisation
# --------------------------------------------------------------------------- #
def _bias_box(calibration):
    """gate_id -> (lo_seconds, hi_seconds), defaulting to an exact zero clock."""
    box = {}
    for gate, span in (calibration or {}).items():
        lo, hi = float(span[0]), float(span[1])
        if lo > hi:
            raise ValueError('invalid_calibration:' + str(gate))
        box[gate] = (lo, hi)
    return box


def _runs(intervals):
    out = []
    for span in intervals or []:
        start, end = parse_ts(span[0]), parse_ts(span[1])
        if end < start:
            raise ValueError('invalid_carousel_interval')
        out.append((start, end))
    return sorted(out)


def build_world(pages, calibration=None, runs=None, anchors=None, threshold=PROMISE_SECONDS):
    """Expand door pages, resets, occlusions and anchors into person slots.

    Returns a dict with entry slots, exit slots, observed counts, the running
    queue projection and the list of evidence problems encountered.  Every slot
    is anonymous; anchors only carry a shared evidence id, never a name.
    """
    bias = _bias_box(calibration)
    runs = _runs(runs)
    entries, exits = [], []
    problems = []
    seq = 0
    seen_observed_in = seen_observed_out = 0
    # Occluded pages are pooled: one blind interval contributes one pool of
    # unobserved entry slots and one pool of unobserved exit slots, never one
    # pool per page, so anonymous people stay count-conserving.  Events seen
    # despite the occlusion keep their own page timestamp; only fully
    # unobserved slots fall back to the interval start/end.
    blind = {'entered': 0, 'exited': 0, 'cap': 0,
             'entry_events': [], 'exit_events': [],
             'start': None, 'end': None, 'gate': None, 'pages': []}

    def add_entry(gate, at_raw, observed, anchor=None, ticket=None):
        nonlocal seq
        seq += 1
        slot = {'kind': 'entry', 'slot': 'E%d' % seq, 'gate': gate,
                'at': parse_ts(at_raw), 'at_raw': at_raw,
                'observed': bool(observed), 'anchor': anchor, 'ticket': ticket}
        entries.append(slot)
        return slot

    def add_exit(gate, at_raw, observed, anchor=None, ticket=None):
        nonlocal seq
        seq += 1
        slot = {'kind': 'exit', 'slot': 'X%d' % seq, 'gate': gate,
                'at': parse_ts(at_raw), 'at_raw': at_raw,
                'observed': bool(observed), 'anchor': anchor, 'ticket': ticket}
        exits.append(slot)
        return slot

    def flush_blind():
        nonlocal seen_observed_in, seen_observed_out
        if not blind['pages']:
            return
        if blind['cap'] < blind['entered'] or blind['cap'] < blind['exited']:
            raise ValueError('invalid_occlusion_capacity_bound')
        for at in blind['entry_events']:
            add_entry(blind['gate'], at, True)
        seen_observed_in += len(blind['entry_events'])
        # Unobserved entries are only known to have happened in the blind
        # interval; the earliest possible time keeps every later pairing open.
        for _ in range(blind['cap'] - len(blind['entry_events'])):
            add_entry(blind['gate'], blind['start'], False)
        for at in blind['exit_events']:
            add_exit(blind['gate'], at, True)
        seen_observed_out += len(blind['exit_events'])
        # Unobserved exits use the latest possible time for the same reason.
        for _ in range(blind['cap'] - len(blind['exit_events'])):
            add_exit(blind['gate'], blind['end'], False)
        blind.update(entered=0, exited=0, cap=0, entry_events=[], exit_events=[],
                     start=None, end=None, gate=None, pages=[])

    for page in sorted(pages, key=lambda p: (parse_ts(p['at']), p.get('page_id', ''))):
        gate = page.get('gate_id', 'G1')
        at = page['at']
        reset = bool(page.get('reset'))
        occluded = bool(page.get('occluded'))
        if occluded:
            ent_lo = int(page.get('entered', 0) or 0)
            ext_lo = int(page.get('exited', 0) or 0)
            cap = int(page.get('capacity_bound', 0) or 0)
            if ent_lo < 0 or ext_lo < 0:
                raise ValueError('invalid_occlusion_page:' + str(page.get('page_id')))
            if blind['pages'] and blind['gate'] != gate:
                flush_blind()
            if not blind['pages']:
                blind.update(start=at, end=at, gate=gate)
            blind['end'] = at
            blind['entered'] += ent_lo
            blind['exited'] += ext_lo
            blind['cap'] = max(blind['cap'], cap)
            blind['entry_events'].extend([at] * ent_lo)
            blind['exit_events'].extend([at] * ext_lo)
            blind['pages'].append(page.get('page_id', at))
            problems.append('occluded:' + str(page.get('page_id', at)))
            continue
        flush_blind()

        entered = int(page.get('entered', 0) or 0)
        exited = int(page.get('exited', 0) or 0)
        if entered < 0 or exited < 0:
            raise ValueError('invalid_tally:' + str(page.get('page_id')))
        if reset:
            # A reset loses the cumulative reading.  The projection restarts
            # here: any negative debt carried by the reset page is absorbed by
            # the reset itself, never turned into fabricated people or a
            # negative-queue anomaly against the fresh counter series.
            problems.append('counter_reset:' + str(page.get('page_id', gate)))
        elif seen_observed_in + entered < seen_observed_out + exited:
            problems.append('negative_queue_projection:' + str(page.get('page_id')))
        for _ in range(entered):
            add_entry(gate, at, True)
        for _ in range(exited):
            add_exit(gate, at, True)
        seen_observed_in += entered
        seen_observed_out += exited
    flush_blind()

    # Continuous-track and ticket anchors explain the *same* people the door
    # pages count.  They bind to door slots at the matching gate/timestamp
    # instead of creating extra people.  Anchors joined by ``co_witness``
    # (an anonymous track and a voluntary ticket watching the same person)
    # share one slot; anchors with no link at the same timestamp are distinct
    # people and take distinct slots.  Only an anchor unexplained by any door
    # observation contributes a new observed slot.
    forced = {}
    entry_pool, exit_pool = {}, {}
    for e in entries:
        entry_pool.setdefault((e['gate'], e['at']), []).append(e)
    for x in exits:
        exit_pool.setdefault((x['gate'], x['at']), []).append(x)
    claimed_entries, claimed_exits = set(), set()
    slot_of_anchor = {}

    def take(pool, claimed, key, add, gate, at_raw):
        slot = next((s for s in pool.get(key, []) if s['slot'] not in claimed), None)
        if slot is None:
            slot = add(gate, at_raw, True)
            pool.setdefault(key, []).append(slot)
        claimed.add(slot['slot'])
        return slot

    for anchor in anchors or []:
        aid = anchor['anchor_id']
        e_gate = anchor.get('entry_gate', 'G1')
        x_gate = anchor.get('exit_gate', 'G1')
        e_key = (e_gate, parse_ts(anchor['entry_at']))
        x_key = (x_gate, parse_ts(anchor['exit_at']))
        linked = anchor.get('co_witness')
        if linked and linked in slot_of_anchor:
            entry, exit_ = slot_of_anchor[linked]
        else:
            entry = take(entry_pool, claimed_entries, e_key,
                         add_entry, e_gate, anchor['entry_at'])
            exit_ = take(exit_pool, claimed_exits, x_key,
                         add_exit, x_gate, anchor['exit_at'])
        slot_of_anchor[aid] = (entry, exit_)
        entry['anchor'] = aid if not entry.get('anchor') else entry['anchor'] + ',' + aid
        exit_['anchor'] = aid if not exit_.get('anchor') else exit_['anchor'] + ',' + aid
        if anchor.get('ticket_no'):
            entry['ticket'] = anchor['ticket_no']
            exit_['ticket'] = anchor['ticket_no']
        forced[entry['slot']] = exit_['slot']

    observed_in, observed_out = seen_observed_in, seen_observed_out
    if len(entries) < len(exits):
        problems.append('more_exits_than_entries')

    return {'entries': entries, 'exits': exits, 'forced': forced, 'bias_box': bias,
            'runs': runs, 'observed_in': observed_in, 'observed_out': observed_out,
            'problems': problems, 'threshold': float(threshold)}


# --------------------------------------------------------------------------- #
# Edge feasibility under a shared per-gate clock offset
# --------------------------------------------------------------------------- #
def _offset_limits(world):
    return {gate: world['bias_box'].get(gate, (0.0, 0.0)) for gate in
            {s['gate'] for s in world['entries'] + world['exits']}}


def edge_wait_interval(world, entry, exit_):
    """Best/worst corrected waiting time over the shared gate offset box."""
    limits = _offset_limits(world)
    e_lo, e_hi = limits[entry['gate']]
    x_lo, x_hi = limits[exit_['gate']]
    # corrected exit - corrected entry = raw gap + ox - oe
    gap = exit_['at'] - entry['at']
    return gap + x_lo - e_hi, gap + x_hi - e_lo


def edge_possible(world, entry, exit_):
    lo, _ = edge_wait_interval(world, entry, exit_)
    if lo < -EPS:
        return False  # nobody can leave before they enter at any shared offset
    if world['runs']:
        limits = _offset_limits(world)
        x_lo, x_hi = limits[exit_['gate']]
        corrected_exit_lo, corrected_exit_hi = exit_['at'] + x_lo, exit_['at'] + x_hi
        if not any(corrected_exit_hi >= run_start - EPS and
                   corrected_exit_lo <= run_end + EPS
                   for run_start, run_end in world['runs']):
            return False  # a pickup while the carousel never runs is impossible
    return True


def edge_certainly_long(world, entry, exit_):
    lo, _ = edge_wait_interval(world, entry, exit_)
    return lo > world['threshold'] + EPS


def edge_possibly_long(world, entry, exit_):
    _, hi = edge_wait_interval(world, entry, exit_)
    return hi > world['threshold'] + EPS


# --------------------------------------------------------------------------- #
# Min-cost / max-cost flow (SPFA) over the bipartite pairing graph
# --------------------------------------------------------------------------- #
def _min_cost_flow(entries, exits, edges, cost):
    """Pair every entry with a distinct exit minimising total edge cost.

    ``edges`` maps entry slot -> list of exit slots.  Raises when no complete
    pairing exists.  Returns a dict entry_slot -> exit_slot.
    """
    n_en, n_ex = len(entries), len(exits)
    S, T = 0, 1
    en_nodes = {e['slot']: 2 + i for i, e in enumerate(entries)}
    ex_nodes = {x['slot']: 2 + n_en + j for j, x in enumerate(exits)}
    N = 2 + n_en + n_ex
    graph = [[] for _ in range(N)]

    def add(u, v, cap, w, tag):
        graph[u].append([v, cap, w, tag, len(graph[v])])
        graph[v].append([u, 0, -w, None, len(graph[u]) - 1])

    for e in entries:
        add(S, en_nodes[e['slot']], 1, 0, None)
    for x in exits:
        add(ex_nodes[x['slot']], T, 1, 0, None)
    for e in entries:
        u = en_nodes[e['slot']]
        for x in edges.get(e['slot'], []):
            add(u, ex_nodes[x['slot']], 1, cost(e, x), (e['slot'], x['slot']))

    flow = 0
    pairing = {}
    INF = float('inf')
    while flow < n_en:
        dist = [INF] * N
        in_queue = [False] * N
        prev_node = [-1] * N
        prev_edge = [-1] * N
        dist[S] = 0
        queue = deque([S])
        while queue:
            u = queue.popleft()
            in_queue[u] = False
            for idx, arc in enumerate(graph[u]):
                v, cap, w, _tag, _rev = arc
                if cap > 0 and dist[v] > dist[u] + w + EPS:
                    dist[v] = dist[u] + w
                    prev_node[v], prev_edge[v] = u, idx
                    if not in_queue[v]:
                        queue.append(v)
                        in_queue[v] = True
        if prev_node[T] < 0:
            raise ValueError('no_feasible_pairing')
        v = T
        while v != S:
            u = prev_node[v]
            arc = graph[u][prev_edge[v]]
            arc[1] -= 1
            graph[v][arc[4]][1] += 1
            if arc[3] is not None:
                e_slot, x_slot = arc[3]
                pairing[e_slot] = x_slot
            v = u
        flow += 1
    return pairing


def _flow(world, predicate, minimise):
    """Extreme number of predicate-satisfying pairs over all feasible pairings."""
    entries, exits = world['entries'], world['exits']
    edges = {}
    for e in entries:
        edges[e['slot']] = [x for x in exits if edge_possible(world, e, x)]
        if not edges[e['slot']]:
            raise ValueError('no_feasible_pairing:' + e['slot'])

    def cost(e, x):
        hit = 1 if predicate(world, e, x) else 0
        return hit if minimise else -hit

    pairing = _min_cost_flow(entries, exits, edges, cost)
    by_entry = {e['slot']: e for e in entries}
    by_exit = {x['slot']: x for x in exits}
    hits = sum(1 for e_slot, x_slot in pairing.items()
               if predicate(world, by_entry[e_slot], by_exit[x_slot]))
    return pairing, hits


# --------------------------------------------------------------------------- #
# Shared per-gate offset realisation for a concrete pairing
# --------------------------------------------------------------------------- #
def _run_offset_domain(world, exit_slot):
    """Offsets of the exit gate for which the corrected exit hits a run."""
    allowed = []
    for start, end in world['runs']:
        allowed.append((start - exit_slot['at'], end - exit_slot['at']))
    return allowed


def _exit_in_run(world, exit_slot, offset):
    corrected = exit_slot['at'] + offset
    return any(start - EPS <= corrected <= end + EPS
               for start, end in world['runs'])


def _intersect_run(domain, allowed):
    lo, hi = domain
    pieces = [(max(lo, a_lo), min(hi, a_hi)) for a_lo, a_hi in allowed]
    pieces = [(a, b) for a, b in pieces if a <= b + EPS]
    return pieces


def realize_offsets(world, pairing):
    """Find ONE gate-offset vector (constant for the whole run) for a pairing.

    Constraints per paired edge: corrected exit >= corrected entry and, when
    carousel runs are known, the corrected exit lies inside a running interval.
    Backtracking tightens per-gate [lo, hi] intervals; an offset is never chosen
    per person.  Returns gate_id -> offset seconds, or None if unrealisable.
    """
    by_entry = {e['slot']: e for e in world['entries']}
    by_exit = {x['slot']: x for x in world['exits']}
    base = _offset_limits(world)
    gates = sorted(base)

    edges = []
    for e_slot, x_slot in pairing.items():
        e, x = by_entry[e_slot], by_exit[x_slot]
        edges.append((e['gate'], x['gate'], x['at'] - e['at'], x))

    def backtrack(i, assign, domains):
        if i == len(gates):
            return dict(assign)
        gate = gates[i]
        lo, hi = domains[gate]
        # Endpoints plus the interval midpoint.  Linear difference constraints
        # are satisfied at an endpoint whenever an interior point satisfies
        # them; the midpoint is a defensive fallback for run-window joins.
        candidates = [v for v in {lo, (lo + hi) / 2.0, hi}
                      if base[gate][0] - EPS <= v <= base[gate][1] + EPS]
        for value in candidates:
            trial = dict(assign)
            trial[gate] = value
            next_domains = {g: list(d) for g, d in domains.items()}
            ok = True
            for ge, gx, gap, exit_slot in edges:
                oe, ox = trial.get(ge), trial.get(gx)
                if oe is not None and ox is not None:
                    if gap + ox - oe < -EPS:
                        ok = False
                        break
                    if world['runs'] and not _exit_in_run(world, exit_slot, ox):
                        ok = False
                        break
                elif oe is not None:
                    # gap + ox - oe >= 0  ->  ox >= oe - gap
                    d_lo, d_hi = next_domains[gx]
                    next_domains[gx] = [max(d_lo, oe - gap), d_hi]
                    if world['runs']:
                        pieces = _intersect_run(next_domains[gx],
                                                _run_offset_domain(world, exit_slot))
                        if not pieces:
                            ok = False
                            break
                        next_domains[gx] = [min(a for a, _ in pieces),
                                            max(b for _, b in pieces)]
                elif ox is not None:
                    # oe <= gap + ox
                    d_lo, d_hi = next_domains[ge]
                    next_domains[ge] = [d_lo, min(d_hi, gap + ox)]
                if any(d[0] > d[1] + EPS for d in next_domains.values()):
                    ok = False
                    break
            if not ok:
                continue
            found = backtrack(i + 1, trial, next_domains)
            if found is not None:
                return found
        return None

    return backtrack(0, {}, {g: list(span) for g, span in base.items()})


# --------------------------------------------------------------------------- #
# Pairing presentation
# --------------------------------------------------------------------------- #
def _describe(world, pairing, offsets):
    by_entry = {e['slot']: e for e in world['entries']}
    by_exit = {x['slot']: x for x in world['exits']}
    rows = []
    for e_slot in sorted(pairing, key=lambda s: by_entry[s]['at']):
        e, x = by_entry[e_slot], by_exit[pairing[e_slot]]
        corrected_wait = (x['at'] + offsets[x['gate']]) - (e['at'] + offsets[e['gate']])
        lo_w, hi_w = edge_wait_interval(world, e, x)
        rows.append({
            'entry_slot': e['slot'], 'exit_slot': x['slot'],
            'entry_gate': e['gate'], 'exit_gate': x['gate'],
            'entry_at': e['at_raw'], 'exit_at': x['at_raw'],
            'entry_observed': e['observed'], 'exit_observed': x['observed'],
            'anchor_id': e['anchor'], 'ticket_no': e['ticket'],
            'wait_seconds_min': round(max(lo_w, 0.0), 3),
            'wait_seconds_max': round(hi_w, 3),
            'realised_wait_seconds': round(corrected_wait, 3),
            'breach': corrected_wait > world['threshold'] + EPS,
        })
    return rows


def _apply_forced(world):
    """Validate anchor-forced pairs and withdraw their slots from the free flow.

    A continuous track that covers one person end to end fixes both endpoints;
    those slots no longer participate in the anonymous global matching.
    """
    by_entry = {e['slot']: e for e in world['entries']}
    by_exit = {x['slot']: x for x in world['exits']}
    used_entries, used_exits = set(), set()
    fixed = {}
    for e_slot, x_slot in world['forced'].items():
        e, x = by_entry[e_slot], by_exit[x_slot]
        if not edge_possible(world, e, x):
            raise ValueError('anchor_pair_impossible:' + str(e.get('anchor')))
        used_entries.add(e_slot)
        used_exits.add(x_slot)
        fixed[e_slot] = x_slot
    free_entries = [e for e in world['entries'] if e['slot'] not in used_entries]
    free_exits = [x for x in world['exits'] if x['slot'] not in used_exits]
    return fixed, free_entries, free_exits


def compute_bounds(world):
    """Return the certificate body for one service window."""
    fixed, free_entries, free_exits = _apply_forced(world)
    balanced = (world['observed_in'] == world['observed_out']
                and len(free_entries) == len(free_exits)
                and len(world['entries']) == len(world['exits']))
    if not balanced:
        # An open window (people still inside) cannot offer complete pairings.
        return {'status': 'open_window',
                'problems': list(world['problems']) + ['incomplete_window'],
                'threshold_seconds': int(world['threshold']),
                'observed_entered': world['observed_in'],
                'observed_exited': world['observed_out'],
                'slots_entered': len(world['entries']),
                'slots_exited': len(world['exits']),
                'lower_bound': 0, 'upper_bound': len(world['entries']),
                'lower_pairing': [], 'upper_pairing': [],
                'lower_gate_offsets_seconds': {}, 'upper_gate_offsets_seconds': {}}
    free_world = dict(world)
    free_world['entries'], free_world['exits'] = free_entries, free_exits

    by_entry = {e['slot']: e for e in world['entries']}
    by_exit = {x['slot']: x for x in world['exits']}

    def fixed_hits(predicate):
        return sum(1 for e_slot, x_slot in fixed.items()
                   if predicate(world, by_entry[e_slot], by_exit[x_slot]))

    lower_pair, lower_hits = _flow(free_world, edge_certainly_long, minimise=True)
    upper_pair, upper_hits = _flow(free_world, edge_possibly_long, minimise=False)

    lower_full, upper_full = dict(fixed), dict(fixed)
    lower_full.update(lower_pair)
    upper_full.update(upper_pair)
    lower = lower_hits + fixed_hits(edge_certainly_long)
    upper = upper_hits + fixed_hits(edge_possibly_long)

    lower_offsets = realize_offsets(world, lower_full)
    upper_offsets = realize_offsets(world, upper_full)
    if lower_offsets is None or upper_offsets is None:
        raise ValueError('pairing_not_realisable_under_shared_clock')

    status = 'ok'
    if world['problems']:
        status = 'evidence_gap'
    if upper > len(world['entries']):
        status = 'infeasible_evidence'

    return {
        'status': status,
        'problems': list(world['problems']),
        'threshold_seconds': int(world['threshold']),
        'observed_entered': world['observed_in'],
        'observed_exited': world['observed_out'],
        'slots_entered': len(world['entries']),
        'slots_exited': len(world['exits']),
        'lower_bound': lower,
        'upper_bound': upper,
        'lower_pairing': _describe(world, lower_full, lower_offsets),
        'upper_pairing': _describe(world, upper_full, upper_offsets),
        'lower_gate_offsets_seconds': {g: round(v, 3) for g, v in lower_offsets.items()},
        'upper_gate_offsets_seconds': {g: round(v, 3) for g, v in upper_offsets.items()},
    }


def window_bounds(pages, calibration=None, runs=None, anchors=None,
                  threshold=PROMISE_SECONDS):
    """Convenience entry point used by tests and the carousel importer."""
    world = build_world(pages, calibration=calibration, runs=runs,
                        anchors=anchors, threshold=threshold)
    return compute_bounds(world)
