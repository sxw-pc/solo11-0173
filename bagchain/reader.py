"""离线读头采集器：从多天线 RFID 读数与条码枪短帧恢复真实过闸轨迹。

管线（全部为确定性纯函数，ingest 只负责落库）：
  1. 校准信标 -> 每个读头每个重启纪元的时钟偏移/漂移；
  2. 原始帧归一化、补传去重、重启纪元校验、CRC 校验；
  3. EPC 标签绑定解析（含标签复用），条码/RFID 身份冲突标记；
  4. 几何过滤：只有本闸本天线、达场强阈值且 CRC 正确的读数可作过闸证据，
     邻闸远距离听到一律标 remote_overhear，不能算过闸；
  5. 逐件 naive 基线（独立取最高可信闸口，保留用于对照测试）；
  6. 全局联合关联：枚举 bag->gate 指派，联合满足单闸容量、传送带方向、
     航班合同与身份锚点；所有可行/不可行指派都作为假设保留，不按文件顺序任取；
  7. 按采集前固定的业务路线（direct_first / anti_missort）决定哪些身份闭合，
     只有闭合 bag_id/flight/station/actor/seen_at 的行李才生成 scan 事件，
     再经 source.import_file('sort') 走原有 sortation.scan/load 投影。
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .source import import_file
from .store import canonical, connect, digest, one

POLICIES = ('direct_first', 'anti_missort')
DIRECT_FIRST = 'direct_first'
ANTI_MISSORT = 'anti_missort'

# ---- 时间 ----------------------------------------------------------------

def parse_ts(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('timestamp_must_be_utc_z')
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def median_dt(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return ordered[mid - 1] + (ordered[mid] - ordered[mid - 1]) / 2

# ---- 1. 时钟校准 ----------------------------------------------------------

def calibrate(batch):
    """按 (reader, restart_seq) 聚合信标，返回校准表与信标帧 id。"""
    beacons = {}
    for f in batch['frames']:
        if f['frame_type'] != 'beacon':
            continue
        key = (f['reader_id'], f['restart_seq'])
        beacons.setdefault(key, []).append(f)
    table = {}
    beacon_ids = []
    for (reader_id, restart_seq), items in beacons.items():
        items = sorted(items, key=lambda x: parse_ts(x['received_at']))
        if len(items) < 2:
            raise ValueError('calibration_needs_two_beacons')
        pts = [(parse_ts(b['device_time']), parse_ts(b['received_at'])) for b in items]
        # corr(t)=received-device；首末两点估漂移，中间点残差取均值校正偏移。
        t0_dev, t0_rec = pts[0]
        t1_dev, t1_rec = pts[-1]
        span = (t1_dev - t0_dev).total_seconds()
        drift = ((t1_rec - t1_dev).total_seconds() - (t0_rec - t0_dev).total_seconds()) / span if span else 0.0
        residuals = [(rec - dev).total_seconds() - drift * (dev - t0_dev).total_seconds()
                     for dev, rec in pts]
        offset = sum(residuals) / len(residuals)
        table[(reader_id, restart_seq)] = {
            'reader_id': reader_id, 'restart_seq': restart_seq,
            'offset_s': offset, 'drift_s_per_s': drift,
            'epoch_start_device': fmt_ts(t0_dev), 'epoch_end_device': fmt_ts(t1_dev),
            'beacon_frame_ids': [b['frame_id'] for b in items],
        }
        beacon_ids.extend(b['frame_id'] for b in items)
    if not table:
        raise ValueError('calibration_beacon_required')
    return table, beacon_ids

def correct_time(cal, reader_id, restart_seq, device_time):
    """返回 (校正时间 or None, 校准记录 or None)。未知重启纪元不给猜测时间。"""
    entry = cal.get((reader_id, restart_seq))
    if entry is None:
        return None, None
    dev = parse_ts(device_time)
    base = parse_ts(entry['epoch_start_device'])
    corrected = dev + timedelta(seconds=entry['offset_s']
                                + entry['drift_s_per_s'] * (dev - base).total_seconds())
    return corrected, entry

# ---- 2/3. 帧归一化、去重、标签解析 ----------------------------------------

def _binding_at(bindings, epc, when):
    active = [b for b in bindings if b['epc'] == epc
              and parse_ts(b['bound_at']) <= when
              and (not b.get('retired_at') or when < parse_ts(b['retired_at']))]
    if len(active) > 1:
        raise ValueError('overlapping_epc_binding')
    return active[0] if active else None

def normalize(batch, cal):
    """去重补传、校正时间、解析 EPC 身份、条码校验、几何角色分类。"""
    gates = {g['gate']: g for g in batch['gates']}
    reader_gate = {g['reader_id']: g['gate'] for g in batch['gates']}
    bindings = batch.get('tag_bindings', [])
    seen, rows = {}, []
    for f in batch['frames']:
        if f['frame_type'] == 'beacon':
            continue
        fid = f['frame_id']
        if fid in seen:  # 断线补传：同 frame_id 重发只记一次，次数进证据。
            seen[fid]['evidence']['retransmit_count'] += 1
            continue
        row = {'frame_id': fid, 'raw': f, 'evidence': {'retransmit_count': 0},
               'usable': False, 'role': None, 'bag_id': None, 'gate': None,
               'corrected_at': None, 'rssi_db': f.get('rssi_db'), 'zone': f.get('zone'),
               'crc_ok': bool(f.get('crc_ok', True))}
        seen[fid] = row
        rows.append(row)

        if f['frame_type'] == 'rfid':
            corrected, entry = correct_time(cal, f['reader_id'], f['restart_seq'], f['device_time'])
            row['corrected_at'] = fmt_ts(corrected) if corrected else None
            if entry is None:
                row['role'] = 'uncalibrated_epoch'
                row['reason'] = 'reader_restart_without_beacon'
                continue
            # CRC 失败也先解析 EPC 身份，审计才能指认是哪件标签碰撞；但帧不可用。
            binding = _binding_at(bindings, f['epc'], corrected)
            if binding is None:
                row['role'] = 'epc_unbound'
                row['reason'] = 'epc_not_active_at_read_time'
                row['evidence']['epc'] = f['epc']
                continue
            row['bag_id'] = binding['bag_id']
            row['evidence']['epc'] = f['epc']
            row['evidence']['tag_reuse_seq'] = binding['tag_reuse_seq']
            if not f.get('crc_ok', True):
                row['role'] = 'crc_failed'
                row['reason'] = 'rfid_checksum_failed'
                continue
            home = reader_gate[f['reader_id']]
            if f['zone'] != home:  # 邻闸区域被本读头听到：远距离串读，永不算过 home 闸。
                row['role'] = 'remote_overhear'
                row['gate'] = f['zone']
                row['reason'] = f'heard_by_{home}_but_zone_{f["zone"]}'
                row['usable'] = True
                continue
            gate = gates[home]
            row['gate'] = home
            if f['rssi_db'] < gate['rssi_at_gate_db']:
                row['role'] = 'weak_gate_read'
                row['reason'] = 'below_at_gate_rssi_threshold'
                continue
            row['role'] = 'gate_read'
            row['reason'] = 'own_zone_at_gate_rssi'
            row['usable'] = True
            row['evidence']['antenna'] = f['antenna']
            continue

        if f['frame_type'] == 'barcode':
            # 多接收点条码帧：逐接收者校正，任一未知纪元则该接收点不可用。
            receiver_times = []
            for rcv in f['receivers']:
                corrected, entry = correct_time(cal, rcv['reader_id'], rcv['restart_seq'],
                                                rcv['device_time'])
                if entry is None:
                    continue
                receiver_times.append((reader_gate[rcv['reader_id']], corrected, rcv['rssi_db'],
                                       rcv['reader_id']))
            if not receiver_times:
                row['role'] = 'uncalibrated_epoch'
                row['reason'] = 'barcode_without_calibrated_receiver'
                continue
            row['corrected_at'] = fmt_ts(median_dt([t for _, t, _, _ in receiver_times]))
            row['evidence']['receivers'] = [
                {'gate': g, 'reader_id': rid, 'corrected_at': fmt_ts(t), 'rssi_db': rssi}
                for g, t, rssi, rid in receiver_times]
            row['evidence']['gate_hint'] = f.get('gate_hint')
            local_gates = {g for g, _, _, _ in receiver_times}
            row['gate'] = f.get('gate_hint') if f.get('gate_hint') in local_gates else None
            row['bag_id'] = f.get('bag_id')
            if not f.get('checksum_ok', True):
                row['role'] = 'barcode_checksum_failed'
                row['reason'] = 'barcode_checksum_failed'
                continue
            # 条码与同时刻 RFID 身份不一致：保留冲突，不允许该条码作身份锚点。
            conflict_epcs = f.get('co_read_epcs', [])
            foreign = set()
            for epc in conflict_epcs:
                binding = _binding_at(bindings, epc, parse_ts(row['corrected_at']))
                if binding and binding['bag_id'] != f['bag_id']:
                    foreign.add(binding['bag_id'])
            row['evidence']['co_read_bags'] = sorted(foreign)
            if foreign:
                row['role'] = 'barcode_rfid_conflict'
                row['reason'] = 'barcode_bag_differs_from_colocated_rfid:' + ','.join(sorted(foreign))
                continue
            row['role'] = 'barcode_anchor'
            row['reason'] = 'checksum_valid_identity_anchor'
            row['evidence']['localized_gates'] = sorted(local_gates)
            row['usable'] = True
            continue

        raise ValueError('unknown_frame_type')
    return rows

# ---- 4/5. 候选过闸与逐件 naive 基线 --------------------------------------

DIRECTION_ORDER = {'IN': 0, 'GATE': 1, 'OUT': 2}

def _crossing_time(reads):
    gate_reads = [r for r in reads if r['raw'].get('antenna') == 'GATE'] or reads
    return min(parse_ts(r['corrected_at']) for r in gate_reads)

def build_candidates(batch, rows):
    """每个 (bag, gate) 汇总本闸证据；远距离串读单独保留不进入候选评分。"""
    gates = {g['gate']: g for g in batch['gates']}
    bags = sorted({r['bag_id'] for r in rows if r['bag_id']})
    candidates = {bag: {} for bag in bags}
    for bag in bags:
        for gate in gates:
            own = [r for r in rows if r['bag_id'] == bag and r['gate'] == gate
                   and r['role'] == 'gate_read']
            barcodes = [r for r in rows if r['bag_id'] == bag and r['role'] == 'barcode_anchor'
                        and gate in r['evidence'].get('localized_gates', [])]
            remote = [r for r in rows if r['bag_id'] == bag and r['role'] == 'remote_overhear'
                      and r['gate'] == gate]
            if not own and not barcodes:
                continue
            antennas = sorted({r['evidence']['antenna'] for r in own},
                              key=lambda a: DIRECTION_ORDER[a])
            monotonic = all(
                DIRECTION_ORDER[own[i]['evidence']['antenna']]
                <= DIRECTION_ORDER[own[i + 1]['evidence']['antenna']]
                for i in range(len(own) - 1)) if len(own) > 1 else True
            candidates[bag][gate] = {
                'gate': gate, 'own_reads': own, 'barcodes': barcodes, 'remote': remote,
                'peak_rssi': max((r['rssi_db'] for r in own), default=None),
                'antennas': antennas, 'monotonic_direction': monotonic,
                'crossing_at': _crossing_time(own) if own else parse_ts(
                    min(r['corrected_at'] for r in barcodes)),
                'frame_ids': sorted([r['frame_id'] for r in own]
                                    + [r['frame_id'] for r in barcodes]),
            }
    return candidates

def naive_per_bag(batch, rows, bag_order):
    """最强常规方案：逐 bag 独立按区域峰值 RSSI 选闸，平分按导入顺序取先者。

    峰值取该 bag 在每个闸区域被任何读头听到的最强一帧（不区分本闸/邻闸读头，
    也不做几何身份锚点），这正是“对每个 bag 取最高 RSSI”的常规实现；
    跨闸口条码对相邻两闸同时成立，无法改变区域峰值的对比。
    之后叠加“同闸只留第一件”补丁。该函数刻意不使用全局容量/合同/方向约束，
    其失败由测试对照断言。
    """
    gate_list = [g['gate'] for g in batch['gates']]
    choices, occupied = {}, set()
    for bag in bag_order:
        scored = []
        for gate in gate_list:
            zone_reads = [r for r in rows if r['bag_id'] == bag and r['gate'] == gate
                          and r['role'] in ('gate_read', 'weak_gate_read', 'remote_overhear')]
            peak = max((r['rssi_db'] for r in zone_reads), default=None)
            score = peak if peak is not None else -999
            scored.append((score, gate))
        scored.sort(key=lambda x: (-x[0], gate_list.index(x[1])))
        top_score, top_gate = scored[0]
        patched = top_gate if top_gate not in occupied else None
        if patched:
            occupied.add(patched)
        choices[bag] = {'gate': top_gate, 'patched_gate': patched,
                        'score': top_score, 'scores': {g: s for s, g in scored}}
    return choices

# ---- 6. 全局联合关联 ------------------------------------------------------

def _enumerate(bags, candidates):
    """枚举所有 bag->gate|None 指派（含不可行指派），全部保留为假设。"""
    results = [{}]
    for bag in bags:
        options = [None] + sorted(candidates[bag])
        results = [dict(assign, **{bag: gate}) for assign in results for gate in options]
    return results

def joint_association(batch, candidates, contracts):
    gates = {g['gate']: g for g in batch['gates']}
    bags = sorted(candidates)
    window_of = {}
    for g in batch['gates']:
        for win in g.get('open_windows', []):
            window_of[g['gate']] = (parse_ts(win['start']), parse_ts(win['end']))

    hypotheses = []
    for assign in _enumerate(bags, candidates):
        used_gate_windows, reasons, score, physical_score = {}, [], 0.0, 0.0
        feasible = True
        flight_ok = True
        for bag, gate in assign.items():
            if gate is None:
                reasons.append(f'{bag}:unassigned')
                feasible = False
                score -= 100
                physical_score -= 100
                continue
            cand = candidates[bag][gate]
            entry = gates[gate]
            bag_score = (cand['peak_rssi'] if cand['peak_rssi'] is not None else -999) \
                + 3 * len(cand['antennas']) + (25 if cand['barcodes'] else 0)
            physical_score += bag_score
            # 航班合同是业务资格，不参与物理可行性：物理上可成立的错航班指派
            # 仍作为假设保留并显式标记，由路线门控与 sortation.scan 下游裁决。
            if contracts.get(bag) and entry['flight'] != contracts[bag]:
                reasons.append(f'{bag}:flight_contract_mismatch@{gate}')
                bag_score -= 500
                flight_ok = False
            if not cand['monotonic_direction']:
                reasons.append(f'{bag}:direction_violation@{gate}')
                bag_score -= 200
                physical_score -= 200
                feasible = False
            if len(cand['antennas']) == 1:
                reasons.append(f'{bag}:single_antenna_weak_direction@{gate}')
                bag_score -= 10
                physical_score -= 10
            crossing = cand['crossing_at']
            win = window_of.get(gate)
            if win and not (win[0] <= crossing <= win[1]):
                reasons.append(f'{bag}:outside_gate_window@{gate}')
                bag_score -= 200
                physical_score -= 200
                feasible = False
            key = (gate, win[0] if win else None)
            if key in used_gate_windows:
                reasons.append(f'{bag}:gate_capacity_violation@{gate}')
                bag_score -= 500
                physical_score -= 500
                feasible = False
            else:
                used_gate_windows[key] = bag
            score += bag_score
        hypotheses.append({'assignment': assign, 'feasible': feasible,
                           'flight_contract_ok': flight_ok, 'score': score,
                           'physical_score': physical_score, 'reasons': reasons})
    # 去重（枚举本身无重复，防御性保留），可行优先、分数高优先，再按指派文本定序。
    unique = {}
    for h in hypotheses:
        key = canonical(h['assignment'])
        unique.setdefault(key, h)
    ordered = sorted(unique.values(),
                     key=lambda h: (-int(h['feasible']), -h['score'], canonical(h['assignment'])))
    for rank, h in enumerate(ordered, start=1):
        h['rank'] = rank
    # 物理可行且完整指派（每个 bag 都过某个闸）才算可采纳的联合解。
    complete_feasible = [h for h in ordered if h['feasible']
                         and all(g is not None for g in h['assignment'].values())]
    return ordered, len(complete_feasible) == 1, complete_feasible[0] if complete_feasible else None

# ---- 7. 路线门控与事件闭合 ------------------------------------------------

def _anchors_for(bag, gate, candidates, rows):
    cand = candidates[bag].get(gate)
    anchors = {'pre_gate_rfid_in': False, 'post_gate_barcode': False,
               'gate_rfid': False, 'out_rfid': False, 'barcode_frame_ids': [],
               'pre_frame_ids': [], 'post_frame_ids': []}
    if not cand:
        return anchors
    for r in cand['own_reads']:
        ant = r['evidence']['antenna']
        if ant == 'IN':
            anchors['pre_gate_rfid_in'] = True
            anchors['pre_frame_ids'].append(r['frame_id'])
        elif ant == 'GATE':
            anchors['gate_rfid'] = True
        elif ant == 'OUT':
            anchors['out_rfid'] = True
    for r in cand['barcodes']:
        anchors['post_gate_barcode'] = True
        anchors['barcode_frame_ids'].append(r['frame_id'])
        anchors['post_frame_ids'].append(r['frame_id'])
    return anchors

def decide(batch, hypotheses, unique, chosen, candidates, rows, policy, contracts):
    if policy not in POLICIES:
        raise ValueError('unknown_policy_must_be_fixed_before_collection')
    gates = {g['gate']: g for g in batch['gates']}
    bags = sorted(candidates)
    decisions = []
    for bag in bags:
        assigned = chosen['assignment'][bag] if (unique and chosen) else None
        anchors = _anchors_for(bag, assigned, candidates, rows) if assigned else \
            {'pre_gate_rfid_in': False, 'post_gate_barcode': False, 'barcode_frame_ids': [],
             'pre_frame_ids': [], 'post_frame_ids': []}
        d = {'bag_id': bag, 'gate': assigned, 'outcome': 'held',
             'flight': None, 'station': None, 'actor': None, 'seen_at': None,
             'reason': None, 'missing_anchor': None, 'anchors': anchors,
             'decisive_frame_ids': [], 'scan_event_id': None, 'load_event_id': None}
        if assigned is None:
            d['reason'] = 'joint_association_not_unique' if not unique else 'no_feasible_gate'
            decisions.append(d)
            continue
        cand = candidates[bag][assigned]
        gate = gates[assigned]
        d['flight'] = gate['flight']
        d['station'] = gate['station']
        d['actor'] = batch['actors'][assigned]
        d['seen_at'] = fmt_ts(cand['crossing_at'])
        d['decisive_frame_ids'] = list(cand['frame_ids'])
        if contracts.get(bag) and gate['flight'] != contracts[bag]:
            d['reason'] = 'held_flight_contract_mismatch'
            decisions.append(d)
            continue
        if policy == DIRECT_FIRST:
            # 唯一联合指派 + 校准后锚定本闸的有效条码：身份闭合。
            if not anchors['post_gate_barcode']:
                d['reason'] = 'held_no_valid_barcode_anchor'
                d['missing_anchor'] = 'post_gate_barcode'
            else:
                d['outcome'] = 'emit_scan'
                d['reason'] = 'unique_joint_trajectory_plus_gate_barcode_anchor'
        else:
            # 防错运：闸前 RFID 与闸后条码两个独立身份锚点，缺一即整批待核。
            missing = [name for name, ok in (('pre_gate_rfid_in', anchors['pre_gate_rfid_in']),
                                             ('post_gate_barcode', anchors['post_gate_barcode']))
                       if not ok]
            if missing:
                d['reason'] = 'held_two_independent_anchors_required'
                d['missing_anchor'] = ','.join(missing)
            else:
                d['outcome'] = 'emit_scan'
                d['reason'] = 'dual_anchor_identity_closed'
        decisions.append(d)
    return decisions

# ---- 事件生成 -------------------------------------------------------------

def _short(sha):
    return sha[:12]

def build_events(batch_sha, policy, batch, decisions):
    """只给身份闭合的行李生成 scan；装机窗存在则生成 load（held 也保留事件由下游裁决）。"""
    short = _short(batch_sha)
    load_windows = {w['flight']: w for w in batch.get('load_windows', [])}
    events = []
    for d in decisions:
        if d['outcome'] != 'emit_scan':
            continue
        gate = next(g for g in batch['gates'] if g['gate'] == d['gate'])
        provenance = {'batch_sha': batch_sha, 'policy': policy, 'gate': d['gate'],
                      'decisive_frame_ids': d['decisive_frame_ids'],
                      'barcode_frame_ids': d['anchors']['barcode_frame_ids'],
                      'reader_ids': [gate['reader_id']]}
        scan_id = f'R-{short}-{policy}-{d["bag_id"]}-{d["gate"]}'
        d['scan_event_id'] = f'{short}-{policy}-SCAN-{d["bag_id"]}'
        events.append({'event_id': d['scan_event_id'], 'kind': 'scan', 'bag_id': d['bag_id'],
                       'scan_id': scan_id, 'station': d['station'], 'flight': d['flight'],
                       'seen_at': d['seen_at'], 'actor': d['actor'],
                       'provenance': provenance})
        window = load_windows.get(d['flight'])
        if window:
            d['load_event_id'] = f'{short}-{policy}-LOAD-{d["bag_id"]}'
            events.append({'event_id': d['load_event_id'], 'kind': 'load',
                           'bag_id': d['bag_id'],
                           'load_id': f'R-{short}-{policy}-{d["bag_id"]}-LOAD',
                           'flight': d['flight'], 'position': window['position'],
                           'confirmed_at': window['closed_at'], 'loader': window['loader'],
                           'provenance': {'batch_sha': batch_sha, 'policy': policy,
                                          'gate': d['gate'], 'requires_accepted_scan': True}})
    return events

# ---- 纯分析（测试可直接调用） ---------------------------------------------

def analyze(batch, contracts, policy, bag_order=None):
    cal, beacon_ids = calibrate(batch)
    rows = normalize(batch, cal)
    candidates = build_candidates(batch, rows)
    bags = sorted(candidates)
    order = bag_order if bag_order is not None else bags
    naive = naive_per_bag(batch, rows, order)
    hypotheses, unique, chosen = joint_association(batch, candidates, contracts)
    decisions = decide(batch, hypotheses, unique, chosen, candidates, rows, policy, contracts)
    unresolved = [{'frame_id': r['frame_id'], 'role': r['role'], 'reason': r['reason'],
                   'bag_id': r['bag_id'], 'gate': r['gate']}
                  for r in rows if not r['usable'] and r['role'] != 'barcode_anchor']
    return {'calibration': cal, 'beacon_ids': beacon_ids, 'rows': rows,
            'candidates': candidates, 'naive': naive, 'hypotheses': hypotheses,
            'unique_solution': unique, 'chosen': chosen, 'decisions': decisions,
            'unresolved': unresolved}

# ---- 持久化与原子提交 ------------------------------------------------------

def _persist(db, batch_sha, policy, batch, result, events, sort_sha):
    seq = db.execute('SELECT COALESCE(MAX(created_seq),0)+1 FROM reader_batch').fetchone()[0]
    db.execute('INSERT INTO reader_batch VALUES(?,?,?,?,?)',
               (batch_sha, policy, json.dumps(batch, ensure_ascii=False).encode('utf-8'),
                sort_sha, seq))
    for entry in result['calibration'].values():
        db.execute('INSERT INTO reader_calibration(batch_sha,policy,reader_id,restart_seq,'
                   'offset_s,drift_s_per_s,beacon_frame_ids,epoch_start_device,epoch_end_device)'
                   ' VALUES(?,?,?,?,?,?,?,?,?)',
                   (batch_sha, policy, entry['reader_id'], entry['restart_seq'],
                    entry['offset_s'], entry['drift_s_per_s'],
                    canonical(entry['beacon_frame_ids']),
                    entry['epoch_start_device'], entry['epoch_end_device']))
    for r in result['rows']:
        db.execute('INSERT INTO reader_frame VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (r['frame_id'], batch_sha, policy, r['raw'].get('reader_id'),
                    r['raw']['frame_type'], r['bag_id'], r['raw'].get('epc'), r['gate'],
                    r['corrected_at'], r['raw'].get('received_at'), r['rssi_db'], r['zone'],
                    int(r['crc_ok']), int(r['usable']), r['role'], r['reason'],
                    canonical(r['evidence'])))
    # hypothesis_no=0 行保存逐件 naive 基线，与联合假设同表可对照。
    for bag, choice in sorted(result['naive'].items()):
        db.execute('INSERT INTO reader_hypothesis(batch_sha,policy,hypothesis_no,bag_id,gate,'
                   'feasible,unique_solution,score,rank_no,assignment_json,reasons_json)'
                   ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (batch_sha, policy, 0, bag, choice['patched_gate'], 0, 0, choice['score'], 0,
                    canonical({'naive': choice}), canonical(['per_bag_independent_baseline'])))
    for no, h in enumerate(result['hypotheses'], start=1):
        for bag, gate in sorted(h['assignment'].items()):
            db.execute('INSERT INTO reader_hypothesis(batch_sha,policy,hypothesis_no,bag_id,gate,'
                       'feasible,unique_solution,score,rank_no,assignment_json,reasons_json)'
                       ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (batch_sha, policy, no, bag, gate, int(h['feasible']),
                        int(result['unique_solution']), h['score'], h['rank'],
                        canonical(h['assignment']), canonical(h['reasons'])))
    for d in result['decisions']:
        db.execute('INSERT INTO reader_decision(batch_sha,policy,bag_id,outcome,gate,flight,'
                   'station,actor,seen_at,scan_event_id,load_event_id,reason,anchors_json,'
                   'decisive_frame_ids,missing_anchor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (batch_sha, policy, d['bag_id'], d['outcome'], d['gate'], d['flight'],
                    d['station'], d['actor'], d['seen_at'], d['scan_event_id'],
                    d['load_event_id'], d['reason'], canonical(d['anchors']),
                    canonical(d['decisive_frame_ids']), d['missing_anchor']))

def _contracts_from_db(db):
    rows = db.execute('SELECT t.bag_id,b.flight FROM bag_tag t JOIN booking b '
                      'ON b.booking_id=t.booking_id').fetchall()
    return {r['bag_id']: r['flight'] for r in rows}

def ingest(db_path, reader_file, policy):
    """采集入口：路线必须在采集前固定；读头审计与 scan/load 投影同一事务原子提交。"""
    if policy not in POLICIES:
        raise ValueError('unknown_policy_must_be_fixed_before_collection')
    raw = Path(reader_file).read_bytes()
    batch = json.loads(raw)
    batch_sha = digest(raw)
    with connect(db_path) as db:
        replay = one(db, 'SELECT created_seq FROM reader_batch WHERE batch_sha=? AND policy=?',
                     (batch_sha, policy))
    if replay:
        return {'batch_sha': batch_sha, 'policy': policy, 'status': 'replayed',
                'replayed_seq': replay['created_seq']}
    with connect(db_path) as db:
        contracts = _contracts_from_db(db)
    result = analyze(batch, contracts, policy)
    events = build_events(batch_sha, policy, batch, result['decisions'])

    with tempfile.TemporaryDirectory(prefix='bagchain-reader-') as folder:
        sort_file = Path(folder) / 'generated-sort.json'
        sort_file.write_text(json.dumps({'events': events}, ensure_ascii=False), encoding='utf-8')
        if events:
            def writer(db, sort_sha, import_results):
                result['import_results'] = import_results
                _persist(db, batch_sha, policy, batch, result, events, sort_sha)
            imported = import_file(db_path, 'sort', sort_file, extra_writer=writer)
        else:
            imported = {'source_sha': None, 'results': []}
            with connect(db_path) as db, db:
                _persist(db, batch_sha, policy, batch, result, events, None)
    return {'batch_sha': batch_sha, 'policy': policy, 'status': 'ingested',
            'unique_solution': result['unique_solution'],
            'emitted_event_ids': [e['event_id'] for e in events],
            'decisions': result['decisions'], 'naive': result['naive'],
            'hypotheses': [{'rank': h['rank'], 'feasible': h['feasible'],
                            'flight_contract_ok': h['flight_contract_ok'], 'score': h['score'],
                            'physical_score': h['physical_score'],
                            'assignment': h['assignment'], 'reasons': h['reasons']}
                           for h in result['hypotheses']],
            'unresolved': result['unresolved'], 'import': imported}
