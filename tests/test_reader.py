"""离线读头采集器测试：原始帧 -> 联合过闸轨迹 -> 真实 scan/load 投影。

覆盖：
  * 原仓可复现链（sort-ok / sort-bad 前后对照，真实跑 sortation.scan/load）；
  * 时钟漂移校准、补传去重、CRC 失败、重启纪元、标签复用、远距离串读；
  * 逐件 RSSI 基线与全局容量约束的真实冲突（两种导入顺序）；
  * 联合关联唯一性、不可判定边界、两条事前固定路线的不同装机结论；
  * 下游 scan/load 原子提交、回滚、幂等重放与 CLI。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bagchain.audit import audit
from bagchain.reader import analyze, calibrate, ingest, normalize
from bagchain.report import report
from bagchain.source import import_file
from bagchain.store import all_rows, connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'bag.sqlite'

    def write(self, name, doc):
        p = Path(self.tmp.name) / name
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        return p

    def book_both_bags(self, db=None):
        db = db or self.db
        import_file(db, 'bag', FIX / 'bag-ok.json')
        import_file(db, 'bag', FIX / 'bag-bag2.json')

    def load_batch(self, name='reader-crossing.json'):
        return json.loads((FIX / name).read_text(encoding='utf-8'))

    # ---- 原仓回归链 -------------------------------------------------------

    def test_legacy_chain_accepted_scan_then_confirmed_load(self):
        """E2 正确航班 -> scan accepted；E2L 消耗该 accepted scan -> load confirmed。"""
        import_file(self.db, 'bag', FIX / 'bag-ok.json')
        r = import_file(self.db, 'sort', FIX / 'sort-ok.json')
        scan_res = next(x for x in r['results'] if x['event_id'] == 'E2')
        load_res = next(x for x in r['results'] if x['event_id'] == 'E2L')
        self.assertEqual((scan_res['status'], scan_res['reason']),
                         ('accepted', 'flight_and_tag_match'))
        self.assertEqual((load_res['status'], load_res['reason']),
                         ('confirmed', 'load_position_committed'))
        rep = report(self.db, 'BAG-1')
        scan = rep['scans'][0]
        self.assertEqual((scan['bag_id'], scan['station'], scan['flight'],
                          scan['seen_at'], scan['actor']),
                         ('BAG-1', 'ORIGIN', 'FL100',
                          '2026-11-22T08:10:00Z', 'sorter-A'))
        self.assertEqual(rep['loads'][0]['status'], 'confirmed')

    def test_legacy_chain_wrong_flight_holds_scan_and_load(self):
        """同一 E2 改错误航班：独立数据库中 scan=held/wrong_flight，load=held/scan_not_accepted。"""
        doc = json.loads((FIX / 'sort-ok.json').read_text(encoding='utf-8'))
        for e in doc['events']:
            if e['event_id'] == 'E2':
                e['flight'] = 'FL999'
        bad = self.write('sort-wrong-flight.json', doc)
        import_file(self.db, 'bag', FIX / 'bag-ok.json')
        r = import_file(self.db, 'sort', bad)
        scan_res = next(x for x in r['results'] if x['event_id'] == 'E2')
        load_res = next(x for x in r['results'] if x['event_id'] == 'E2L')
        self.assertEqual((scan_res['status'], scan_res['reason']), ('held', 'wrong_flight'))
        self.assertEqual((load_res['status'], load_res['reason']),
                         ('held', 'scan_not_accepted'))
        rep = report(self.db, 'BAG-1')
        self.assertEqual(rep['scans'][0]['status'], 'held')
        self.assertEqual(rep['loads'][0]['status'], 'held')

    # ---- 时钟校准与帧卫生 -------------------------------------------------

    def test_clock_drift_calibration_and_out_of_order_dedup(self):
        batch = self.load_batch()
        cal, beacon_ids = calibrate(batch)
        # R101 设备时间快 20 秒；R201 慢 15 秒。
        self.assertAlmostEqual(cal[('R101', 1)]['offset_s'], -20.0)
        self.assertAlmostEqual(cal[('R201', 3)]['offset_s'], 15.0)
        rows = normalize(batch, cal)
        by_id = {r['frame_id']: r for r in rows}
        # 乱序补传的同 frame_id 只保留一行，重传次数进入证据。
        self.assertEqual(len([r for r in rows if r['frame_id'] == 'rf-102-g2-gate']), 1)
        self.assertEqual(by_id['rf-102-g2-gate']['evidence']['retransmit_count'], 1)
        # 校正后的真实过闸时间都落在 08:10:00Z，无论设备时钟偏差。
        self.assertEqual(by_id['rf-101-g1-gate']['corrected_at'],
                         '2026-11-22T08:10:00Z')
        self.assertEqual(by_id['rf-202-g2-gate']['corrected_at'],
                         '2026-11-22T08:10:00Z')

    def test_clock_drift_rate_is_estimated_per_restart_epoch(self):
        # 设备时钟 = 真实 + 20s 固定偏移，另带每秒 -1/600s 的漂移。
        batch = {'frames': [
            {'frame_id': 'x1', 'frame_type': 'beacon', 'reader_id': 'R', 'restart_seq': 1,
             'device_time': '2026-11-22T08:00:20Z', 'received_at': '2026-11-22T08:00:00Z'},
            {'frame_id': 'x2', 'frame_type': 'beacon', 'reader_id': 'R', 'restart_seq': 1,
             'device_time': '2026-11-22T08:10:20Z', 'received_at': '2026-11-22T08:09:59Z'}]}
        from bagchain.reader import correct_time
        cal, _ = calibrate(batch)
        self.assertAlmostEqual(cal[('R', 1)]['offset_s'], -20.0)
        self.assertAlmostEqual(cal[('R', 1)]['drift_s_per_s'], -1 / 600, places=6)
        corrected, _ = correct_time(cal, 'R', 1, '2026-11-22T08:10:20Z')
        self.assertEqual(corrected.strftime('%H:%M:%S'), '08:09:59')

    def test_missing_beacon_rejects_batch_instead_of_guessing(self):
        batch = self.load_batch()
        # R101 只剩一个信标，无法估计漂移/偏移：整批拒绝而不是给猜测时钟。
        batch['frames'] = [f for f in batch['frames'] if f['frame_id'] != 'b-r101-2']
        with self.assertRaisesRegex(ValueError, 'calibration_needs_two_beacons'):
            calibrate(batch)

    def test_crc_failure_keeps_identity_but_unusable(self):
        batch = self.load_batch()
        cal, _ = calibrate(batch)
        rows = normalize(batch, cal)
        failed = next(r for r in rows if r['frame_id'] == 'rf-103-crc-fail')
        self.assertEqual(failed['role'], 'crc_failed')
        self.assertFalse(failed['usable'])
        # 碰撞帧仍可指认是哪件标签，审计能追到来源。
        self.assertEqual(failed['bag_id'], 'BAG-1')

    def test_unknown_reader_restart_is_not_guessed(self):
        batch = self.load_batch()
        cal, _ = calibrate(batch)
        rows = normalize(batch, cal)
        bad = next(r for r in rows if r['frame_id'] == 'rf-203-unknown-epoch')
        self.assertEqual(bad['role'], 'uncalibrated_epoch')
        self.assertIsNone(bad['corrected_at'])
        self.assertFalse(bad['usable'])

    def test_tag_reuse_retires_old_epc(self):
        batch = self.load_batch()
        cal, _ = calibrate(batch)
        rows = normalize(batch, cal)
        # EPC-OLD-1 在 08:05 已退役，08:09 的旧标签读数不能绑定任何现行行李。
        old = next(r for r in rows if r['frame_id'] == 'rf-204-old-epc')
        self.assertEqual(old['role'], 'epc_unbound')
        self.assertIsNone(old['bag_id'])

    def test_remote_overhear_never_counts_as_gate_crossing(self):
        batch = self.load_batch()
        result = analyze(batch, {'BAG-1': 'FL100', 'BAG-2': 'FL200'}, 'direct_first')
        over = [r for r in result['rows'] if r['role'] == 'remote_overhear']
        self.assertTrue(over)
        for r in over:
            self.assertFalse(r['gate'] == r['raw']['zone'] and r['role'] == 'gate_read')
        # 邻闸串读不进入任何候选的本闸读数集合。
        for bag, gates in result['candidates'].items():
            for gate, cand in gates.items():
                self.assertTrue(all(r['role'] == 'gate_read' for r in cand['own_reads']))

    def test_barcode_checksum_and_rfid_conflict_are_retained_not_guessed(self):
        batch = self.load_batch()
        result = analyze(batch, {'BAG-1': 'FL100', 'BAG-2': 'FL200'}, 'direct_first')
        roles = {u['frame_id']: (u['role'], u['reason']) for u in result['unresolved']}
        self.assertEqual(roles['bc-2'][0], 'barcode_checksum_failed')
        self.assertEqual(roles['bc-3'][0], 'barcode_rfid_conflict')
        self.assertIn('BAG-1', roles['bc-3'][1])
        # 冲突/失败条码不得成为 BAG-2 的身份锚点。
        b2 = next(d for d in result['decisions'] if d['bag_id'] == 'BAG-2')
        self.assertFalse(b2['anchors']['post_gate_barcode'])

    # ---- 逐件基线 vs 全局约束 ---------------------------------------------

    def test_naive_per_bag_scheme_clashes_with_gate_capacity(self):
        batch = self.load_batch()
        contracts = {'BAG-1': 'FL100', 'BAG-2': 'FL200'}
        for order in (['BAG-1', 'BAG-2'], ['BAG-2', 'BAG-1']):
            result = analyze(batch, contracts, 'direct_first', bag_order=order)
            # 两件行李的局部峰值都落在 G2，独立选闸必然同闸，违反一闸一件容量。
            self.assertEqual(result['naive']['BAG-1']['gate'], 'G2')
            self.assertEqual(result['naive']['BAG-2']['gate'], 'G2')
            raw_gates = [c['gate'] for c in result['naive'].values()]
            self.assertEqual(raw_gates.count('G2'), 2)
            # “同闸只留第一件”补丁：第二件被挤出，且先到者随导入顺序变化。
            patched = {b: c['patched_gate'] for b, c in result['naive'].items()}
            self.assertEqual(len({g for g in patched.values() if g}), 1)
            if order[0] == 'BAG-1':
                self.assertEqual(patched, {'BAG-1': 'G2', 'BAG-2': None})
            else:
                self.assertEqual(patched, {'BAG-2': 'G2', 'BAG-1': None})

    def test_joint_association_recovers_unique_global_trajectory(self):
        batch = self.load_batch()
        result = analyze(batch, {'BAG-1': 'FL100', 'BAG-2': 'FL200'}, 'direct_first')
        self.assertTrue(result['unique_solution'])
        self.assertEqual(result['chosen']['assignment'],
                         {'BAG-1': 'G1', 'BAG-2': 'G2'})
        # 相反关联作为假设被保留并显式标红：容量冲突或航班合同不符，绝不静默丢弃。
        swapped = next(h for h in result['hypotheses']
                       if h['assignment'] == {'BAG-1': 'G2', 'BAG-2': 'G2'})
        self.assertFalse(swapped['feasible'])
        self.assertTrue(any('gate_capacity_violation' in x for x in swapped['reasons']))
        cross = next((h for h in result['hypotheses']
                      if h['assignment'] == {'BAG-1': 'G2', 'BAG-2': 'G1'}), None)
        if cross is not None:
            self.assertTrue(any('flight_contract_mismatch' in x for x in cross['reasons']))

    def test_ambiguous_batch_keeps_multiple_hypotheses_and_emits_nothing(self):
        batch = self.load_batch('reader-ambiguous.json')
        result = analyze(batch, {'BAG-1': 'FL100', 'BAG-2': 'FL200'}, 'direct_first')
        self.assertFalse(result['unique_solution'])
        feasible = [h for h in result['hypotheses'] if h['feasible']
                    and all(g is not None for g in h['assignment'].values())]
        # 两条全局关联都物理可行且物理证据同分：不得按文件顺序任取一条；
        # 航班合同只作业务标记，不参与物理可行性的对称性。
        self.assertGreaterEqual(len(feasible), 2)
        self.assertEqual(len({h['physical_score'] for h in feasible}), 1)
        contracts_violated = [h for h in feasible if not h['flight_contract_ok']]
        self.assertTrue(contracts_violated)
        self.assertTrue(all(d['outcome'] == 'held' for d in result['decisions']))
        self.assertTrue(all('not_unique' in d['reason'] for d in result['decisions']))

    # ---- 两条事前固定路线 -------------------------------------------------

    def test_direct_first_route_closes_bag1_and_blocks_bag2(self):
        self.book_both_bags()
        r = ingest(self.db, FIX / 'reader-crossing.json', 'direct_first')
        self.assertEqual(r['status'], 'ingested')
        self.assertTrue(r['unique_solution'])
        # 只有 BAG-1 身份闭合；BAG-2 因有效条码锚点不足待核，不产生任何事件。
        self.assertEqual(r['emitted_event_ids'],
                         [e for e in r['emitted_event_ids'] if 'BAG-1' in e])
        rep1, rep2 = report(self.db, 'BAG-1'), report(self.db, 'BAG-2')
        self.assertEqual(len(rep1['scans']), 1)
        scan = rep1['scans'][0]
        self.assertEqual((scan['status'], scan['flight'], scan['station'],
                          scan['seen_at'], scan['actor']),
                         ('accepted', 'FL100', 'ORIGIN',
                          '2026-11-22T08:10:00Z', 'sorter-A'))
        self.assertEqual(rep1['loads'][0]['status'], 'confirmed')
        self.assertEqual(rep2['scans'], [])
        self.assertEqual(rep2['loads'], [])
        d2 = rep2['reader_decisions'][0]
        self.assertEqual((d2['outcome'], d2['missing_anchor']),
                         ('held', 'post_gate_barcode'))

    def test_anti_missort_route_holds_whole_batch_with_specific_missing_anchor(self):
        self.book_both_bags()
        ingest(self.db, FIX / 'reader-crossing.json', 'anti_missort')
        rep1, rep2 = report(self.db, 'BAG-1'), report(self.db, 'BAG-2')
        # 整批待核：BAG-1 缺闸前独立 RFID 锚点（IN 帧碰撞 CRC 失败）。
        d1 = rep1['reader_decisions'][0]
        self.assertEqual(d1['outcome'], 'held')
        self.assertEqual(d1['missing_anchor'], 'pre_gate_rfid_in')
        self.assertEqual(d1['reason'], 'held_two_independent_anchors_required')
        # BAG-2 缺闸后条码锚点（校验失败 + 身份冲突）。
        d2 = rep2['reader_decisions'][0]
        self.assertEqual(d2['missing_anchor'], 'post_gate_barcode')
        # 未闭合身份 -> 不生成 scan；即便有 FL100 装机窗，BAG-1 的 load 也不得存在。
        self.assertEqual(rep1['scans'], [])
        self.assertEqual(rep1['loads'], [])
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence "
                                        "WHERE kind IN ('scan','load')").fetchone()[0], 0)

    def test_policy_must_be_fixed_before_collection(self):
        self.book_both_bags()
        with self.assertRaisesRegex(ValueError, 'unknown_policy'):
            ingest(self.db, FIX / 'reader-crossing.json', 'tune_threshold_later')

    # ---- 原子提交、回滚与审计 ---------------------------------------------

    def test_reader_audit_rows_and_scan_share_one_transaction(self):
        self.book_both_bags()
        r = ingest(self.db, FIX / 'reader-crossing.json', 'direct_first')
        with connect(self.db) as db:
            batch = db.execute('SELECT * FROM reader_batch').fetchone()
            self.assertIsNotNone(batch['sort_source_sha'])
            # 决定性帧、校准与全部假设都可审计。
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reader_calibration').fetchone()[0], 2)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM reader_hypothesis').fetchone()[0],
                               len(r['hypotheses']))
            frames = all_rows(db, 'SELECT * FROM reader_frame')
            self.assertTrue(any(f['role'] == 'gate_read' for f in frames))
            self.assertTrue(any(f['role'] == 'crc_failed' for f in frames))
        # scan 决定携带决定性帧出处，可逐帧追溯。
        scan_decision = next(d for d in report(self.db, 'BAG-1')['decisions']
                             if d['event_id'].endswith('SCAN-BAG-1'))
        provenance = json.loads(scan_decision['provenance'])
        self.assertIn('decisive_frame_ids', provenance)
        self.assertIn('rf-101-g1-gate', provenance['decisive_frame_ids'])
        self.assertIn('bc-1', provenance['barcode_frame_ids'])

    def test_projection_failure_rolls_back_reader_evidence(self):
        # 不导入 booking：生成的 scan 在 sortation 内抛 unknown_bag，
        # 同一事务里的读头审计行必须一起回滚，不能留下半条轨迹。
        with self.assertRaisesRegex(ValueError, 'unknown_bag'):
            ingest(self.db, FIX / 'reader-crossing.json', 'direct_first')
        with connect(self.db) as db:
            for table in ('reader_batch', 'reader_frame', 'reader_decision',
                          'reader_hypothesis', 'reader_calibration', 'evidence', 'scan'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0,
                                 table)

    def test_reader_replay_is_idempotent_and_audit_rebuilds(self):
        self.book_both_bags()
        first = ingest(self.db, FIX / 'reader-crossing.json', 'direct_first')
        again = ingest(self.db, FIX / 'reader-crossing.json', 'direct_first')
        self.assertEqual(again['status'], 'replayed')
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reader_batch').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scan').fetchone()[0], 1)
        self.assertEqual(audit(self.db)['status'], 'ok')

    def test_ambiguous_ingest_persists_audit_without_business_events(self):
        self.book_both_bags()
        r = ingest(self.db, FIX / 'reader-ambiguous.json', 'direct_first')
        self.assertEqual(r['emitted_event_ids'], [])
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reader_batch').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence "
                                        "WHERE kind='scan'").fetchone()[0], 0)
        self.assertEqual(audit(self.db)['status'], 'ok')

    def test_same_batch_two_policies_are_separate_audit_chains(self):
        self.book_both_bags()
        a = ingest(self.db, FIX / 'reader-crossing.json', 'direct_first')
        b = ingest(self.db, FIX / 'reader-crossing.json', 'anti_missort')
        self.assertEqual(a['status'], b['status'], 'ingested')
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reader_batch').fetchone()[0], 2)
            # 只有 direct_first 产生业务事件；anti_missort 不重复装机。
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scan WHERE status='accepted'")
                             .fetchone()[0], 1)

    # ---- CLI --------------------------------------------------------------

    def test_cli_reader_import_and_report(self):
        def run(*args):
            p = subprocess.run([sys.executable, '-m', 'bagchain', *args, '--db', str(self.db)],
                               capture_output=True, text=True, cwd=FIX.parent)
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        run('bag-import', str(FIX / 'bag-ok.json'))
        run('bag-import', str(FIX / 'bag-bag2.json'))
        out = run('reader-import', str(FIX / 'reader-crossing.json'),
                  '--policy', 'direct_first')
        self.assertEqual(out['status'], 'ingested')
        rep = run('report', '--bag', 'BAG-1')
        self.assertEqual(rep['scans'][0]['flight'], 'FL100')
        self.assertTrue(rep['reader_decisions'])
        self.assertEqual(run('audit')['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
