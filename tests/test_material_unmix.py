"""Tests for bounded multi-channel material unmixing.

The fixtures and assertions encode the evidentiary rules of the exercise:

* a nearest-material label is provisional and changes when the reference library
  changes, but never proves bag contents;
* one three-channel reading can have two zero-residual decompositions with
  different restricted-material fractions, so both must be retained;
* one device calibration is shared by every voxel of a batch and cannot be
  chosen per voxel;
* a fourth-channel rescan only clears unoccluded voxels, an outer-pocket swab
  cannot be extrapolated inward, and every published result version is retained;
* audit replay from original bytes detects any tampering.
"""
import json
import tempfile
import unittest
from pathlib import Path

from bagchain.audit import audit
from bagchain.material_unmix import (
    bag_fraction_bounds,
    best_simplex_fit,
    fourth_channel_weights,
    independent_bag_bounds,
    linprog,
    null_direction,
    voxel_fraction_bounds,
)
from bagchain.report import report
from bagchain.source import import_file
from bagchain.store import connect

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / 'fixtures'

R = [4.0, 6.0, 2.0]
P = [7.0, 2.0, 5.0]
C = [5.5, 4.0, 3.5]
REFS = [R, P, C]
NAMES = ['R', 'P', 'C']


def booking_event(bag='BAG-2', booking='BK-2'):
    return {'event_id': 'B-' + bag, 'kind': 'booking', 'bag_id': bag,
            'booking_id': booking, 'passenger': 'P-2', 'flight': 'FL200',
            'destination': 'DEST', 'checked_at': '2026-11-22T08:00:00Z',
            'promised_at': '2026-11-22T09:00:00Z',
            'items': ['clothes'], 'weight_kg': 10}


def write_events(folder, name, events):
    path = Path(folder) / name
    path.write_text(json.dumps({'events': events}), encoding='utf-8')
    return path


class SimplexEngineTests(unittest.TestCase):
    def test_two_phase_simplex_matches_known_optimum(self):
        # max w_R on the unit simplex with the exact C reading: range [0, 0.5]
        A = []
        b = []
        for j in range(3):
            A.append([REFS[i][j] for i in range(3)])
            b.append(C[j])
            A.append([-REFS[i][j] for i in range(3)])
            b.append(-C[j])
        A.append([1.0, 1.0, 1.0])
        b.append(1.0)
        A.append([-1.0, -1.0, -1.0])
        b.append(-1.0)
        low = linprog([1.0, 0.0, 0.0], A, b)
        high = linprog([-1.0, 0.0, 0.0], A, b)
        self.assertAlmostEqual(low[0], 0.0, places=9)
        self.assertAlmostEqual(high[0], 0.5, places=9)

    def test_simplex_reports_infeasible_reading(self):
        # [5,5,3] is not on the simplex; strips with zero bias cannot fit it.
        self.assertIsNone(voxel_fraction_bounds([5.0, 5.0, 3.0], REFS, 0.0, 1.0, 1.0))


class NullDirectionAndAmbiguityTests(unittest.TestCase):
    def test_midpoint_reference_creates_one_null_direction(self):
        direction = null_direction(REFS)
        self.assertIsNotNone(direction)
        # R + P - 2C = 0 with coefficients summing to zero.
        for j in range(3):
            self.assertAlmostEqual(
                direction[0] * R[j] + direction[1] * P[j] + direction[2] * C[j],
                0.0, places=9)
        self.assertAlmostEqual(sum(direction), 0.0, places=9)

    def test_identical_reading_two_worlds_zero_residual(self):
        # 100% C and a 50/50 R/P packing produce the same reading.
        half = [0.5 * R[j] + 0.5 * P[j] for j in range(3)]
        self.assertEqual(half, C)
        residual_c, weights_c = best_simplex_fit(C, REFS)
        residual_hp, _ = best_simplex_fit(half, REFS)
        self.assertAlmostEqual(residual_c, 0.0, places=9)
        self.assertAlmostEqual(residual_hp, 0.0, places=9)
        # The two worlds have R fractions 0 and 0.5 and both must be feasible.
        rng = voxel_fraction_bounds(C, REFS, 0.0, 1.0, 1.0)
        self.assertIsNotNone(rng)
        self.assertAlmostEqual(rng[0], 0.0, places=9)
        self.assertAlmostEqual(rng[1], 0.5, places=9)

    def test_two_worlds_share_sha_nearest_label_and_nnls_residual(self):
        # The exact counter-example from the exercise: identical channel arrays,
        # identical hash and single-material nearest neighbour, but R fractions
        # 0 versus 0.5.  Picking either zero-residual decomposition would write
        # "excluded" in one real packing and "confirmed" in the other.
        import hashlib
        import math
        world_c = {'weights': [0.0, 0.0, 1.0], 'r_fraction': 0.0}
        world_hp = {'weights': [0.5, 0.5, 0.0], 'r_fraction': 0.5}
        readings = []
        for world in (world_c, world_hp):
            reading = [sum(world['weights'][i] * REFS[i][j] for i in range(3))
                       for j in range(3)]
            readings.append(reading)
        self.assertEqual(readings[0], readings[1])
        self.assertEqual(hashlib.sha256(
            json.dumps(readings[0], sort_keys=True).encode()).hexdigest(),
            hashlib.sha256(
                json.dumps(readings[1], sort_keys=True).encode()).hexdigest())
        # Euclidean nearest single material is C for both.
        for reading in readings:
            distances = [(math.sqrt(sum((reading[j] - ref[j]) ** 2
                                        for j in range(3))), name)
                         for ref, name in zip(REFS, NAMES)]
            self.assertEqual(min(distances)[1], 'C')
        # A non-negative least squares style fit has zero residual in both, so
        # residual alone cannot separate the packings; only the interval can.
        for reading in readings:
            residual, _ = best_simplex_fit(reading, REFS)
            self.assertAlmostEqual(residual, 0.0, places=9)
        rng = voxel_fraction_bounds(readings[0], REFS, 0.0, 1.0, 1.0)
        self.assertAlmostEqual(rng[0], world_c['r_fraction'], places=9)
        self.assertAlmostEqual(rng[1], world_hp['r_fraction'], places=9)

    def test_pure_materials_have_point_intervals(self):
        self.assertEqual(
            tuple(round(x, 9) for x in voxel_fraction_bounds(P, REFS, 0.0, 1.0, 1.0)),
            (0.0, 0.0))
        self.assertEqual(
            tuple(round(x, 9) for x in voxel_fraction_bounds(R, REFS, 0.0, 1.0, 1.0)),
            (1.0, 1.0))


class SharedCalibrationTests(unittest.TestCase):
    def _voxels(self):
        return [
            {'channels': C, 'thickness': 1.0, 'volume': 1.0, 'occluded': False},
            {'channels': [0.25 * R[j] + 0.75 * P[j] for j in range(3)],
             'thickness': 1.0, 'volume': 1.0, 'occluded': False},
        ]

    def test_shared_interval_is_nested_in_independent_sum(self):
        voxels = self._voxels()
        shared = bag_fraction_bounds(voxels, REFS, 0.05, 0.01, [1.0, 1.0])[:2]
        independent = independent_bag_bounds(voxels, REFS, 0.05, 0.01)
        # The shared feasible region is a subset, so its interval is nested.
        self.assertLessEqual(independent[0] - 1e-9, shared[0])
        self.assertLessEqual(shared[1], independent[1] + 1e-9)

    def test_one_shared_bias_is_used_for_the_whole_batch(self):
        # Re-solve the joint program and confirm it carries a single bias block
        # rather than one bias vector per voxel.
        from bagchain.material_unmix import _joint_program
        voxels = self._voxels()
        A, b, n_vars = _joint_program(
            voxels, REFS, 0.05, lambda v: 0.99, lambda v: 1.01)
        # 2 voxels * 3 scales + one 3-channel bias split (b+ and b-).
        self.assertEqual(n_vars, 2 * 3 + 2 * 3)
        # Each voxel's six channel-strip rows reference the SAME six shared
        # bias columns (rows are interleaved with thickness constraints, so
        # collect the non-zero bias rows from each block).
        bias_pattern = {(1.0, 0.0, 0.0, -1.0, 0.0, 0.0),
                        (-1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
                        (0.0, 1.0, 0.0, 0.0, -1.0, 0.0),
                        (0.0, -1.0, 0.0, 0.0, 1.0, 0.0),
                        (0.0, 0.0, 1.0, 0.0, 0.0, -1.0),
                        (0.0, 0.0, -1.0, 0.0, 0.0, 1.0)}
        first_bias = {tuple(row[6:]) for row in A[:8] if any(row[6:])}
        second_bias = {tuple(row[6:]) for row in A[8:16] if any(row[6:])}
        self.assertEqual(first_bias, bias_pattern)
        self.assertEqual(second_bias, bias_pattern)
        # Scale columns stay strictly inside each voxel's own block.
        self.assertTrue(all(row[3:6] == [0.0, 0.0, 0.0] for row in A[:8]))
        self.assertTrue(all(row[0:3] == [0.0, 0.0, 0.0] for row in A[8:16]))

    def test_occluded_voxel_spans_full_fraction_range(self):
        voxels = self._voxels() + [
            {'channels': R, 'thickness': 1.0, 'volume': 2.0, 'occluded': True}]
        lo, hi, occluded_volume, clear_volume = bag_fraction_bounds(
            voxels, REFS, 0.05, 0.01, [1.0, 1.0, 2.0])
        self.assertEqual(occluded_volume, 2.0)
        self.assertEqual(clear_volume, 2.0)
        # The occluded volume contributes its full [0, 1] range on top of the
        # clear-voxel interval, so the upper bound strictly exceeds the clear one.
        clear_hi = bag_fraction_bounds(
            self._voxels(), REFS, 0.05, 0.01, [1.0, 1.0])[1]
        self.assertAlmostEqual(hi, (clear_hi * 2.0 + 2.0) / 4.0, places=9)
        self.assertGreater(hi, clear_hi)
        self.assertGreaterEqual(lo, 0.0)


class FourthChannelTests(unittest.TestCase):
    def test_fourth_channel_distinguishes_c_from_rp_half(self):
        refs4 = [3.0, 8.0, 5.0]  # R4 - 2 C4 + P4 = 1 != 0
        weights_c, residual_c = fourth_channel_weights(C, 5.0, REFS, refs4)
        weights_hp, residual_hp = fourth_channel_weights(C, 5.5, REFS, refs4)
        self.assertAlmostEqual(residual_c, 0.0, places=9)
        self.assertAlmostEqual(residual_hp, 0.0, places=9)
        self.assertEqual([round(w, 9) for w in weights_c], [0.0, 0.0, 1.0])
        self.assertEqual([round(w, 9) for w in weights_hp], [0.5, 0.5, 0.0])

    def test_rank_deficient_fourth_channel_rejected(self):
        # C4 chosen on the R/P midpoint line keeps the null direction alive.
        with self.assertRaises(ValueError):
            fourth_channel_weights(C, 5.5, REFS, [3.0, 8.0, 5.5])


class LedgerFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'bag.sqlite'
        import_file(self.db, 'bag',
                    write_events(self.tmp.name, 'bag.json', [booking_event()]))
        import_file(self.db, 'screen', FIX / 'screen-mixture.json')

    def test_three_channel_keeps_bounded_interval_and_residuals(self):
        rep = report(self.db, 'BAG-2')
        states = {v['voxel_id']: v for v in rep['unmix_voxels']}
        self.assertEqual(states['V1']['state'], 'bounded')
        self.assertAlmostEqual(states['V1']['residual'], 0.0, places=9)
        # The ambiguous voxel keeps the full [0, 0.5] R range (error box only
        # widens it slightly).
        self.assertAlmostEqual(states['V1']['r_fraction_lo'], 0.0, places=6)
        self.assertGreater(states['V1']['r_fraction_hi'], 0.5)
        self.assertEqual(states['V3']['state'], 'occluded_pending')
        self.assertIsNone(states['V3']['r_fraction_lo'])
        result = rep['unmix_results'][0]
        self.assertEqual(result['route'], 'three_channel')
        self.assertEqual(json.loads(result['rescan_positions_json']), ['metal-shell'])
        # Booking declaration is retained verbatim and never overwritten.
        self.assertEqual(rep['bag']['declared_items'], 'clothes')
        # Provisional nearest label stays provisional.
        self.assertEqual(rep['screen_labels'][0]['state'], 'provisional')
        self.assertEqual(rep['screen_labels'][0]['material'], 'C')
        # Original voxel rows are immutable evidence once captured.
        self.assertEqual(states['V1']['occluded'], 0)

    def test_route_a_clears_unoccluded_but_keeps_metal_shadow_pending(self):
        rep = report(self.db, 'BAG-2')
        route_a = next(r for r in rep['unmix_results'] if r['route'] == 'A_fourth_channel')
        self.assertEqual(route_a['confirmed_volume'], 2.0)
        self.assertEqual(json.loads(route_a['rescan_positions_json']), ['metal-shell'])
        # The unique decomposition is kept in the followup evidence; original
        # three-channel voxel rows remain immutable.
        followup = rep['unmix_followups'][0]
        evidence = json.loads(followup['evidence_json'])['evidence']
        self.assertEqual(evidence['V1']['state'], 'unique')
        self.assertAlmostEqual(evidence['V1']['r_fraction'], 0.0, places=9)
        self.assertAlmostEqual(evidence['V2']['r_fraction'], 0.25, places=9)
        self.assertEqual(evidence['V3']['state'], 'occluded_pending')
        states = {v['voxel_id']: v for v in rep['unmix_voxels']}
        self.assertEqual(states['V1']['state'], 'bounded')
        self.assertEqual(states['V3']['state'], 'occluded_pending')
        # Occluded voxel still cannot be written off as safe.
        self.assertAlmostEqual(route_a['r_fraction_hi'],
                               (0.25 + 2.0) / 4.0, places=9)

    def test_route_b_swab_does_not_change_inner_fraction(self):
        rep = report(self.db, 'BAG-2')
        route_a = next(r for r in rep['unmix_results']
                       if r['route'] == 'A_fourth_channel')
        route_b = next(r for r in rep['unmix_results']
                       if r['route'] == 'B_pocket_swab')
        # Same interval republished: an outer swab cannot infer inner content.
        self.assertEqual(route_b['r_fraction_lo'], route_a['r_fraction_lo'])
        self.assertEqual(route_b['r_fraction_hi'], route_a['r_fraction_hi'])
        self.assertEqual(route_b['result_version'], route_a['result_version'] + 1)

    def test_all_result_versions_are_retained_side_by_side(self):
        rep = report(self.db, 'BAG-2')
        versions = [(r['route'], r['result_version']) for r in rep['unmix_results']]
        self.assertEqual(versions, [
            ('three_channel', 1),
            ('A_fourth_channel', 2),
            ('B_pocket_swab', 3),
        ])

    def test_audit_replay_is_ok_after_all_routes(self):
        self.assertEqual(audit(self.db)['status'], 'ok')

    def test_no_route_writes_an_unevidenced_voxel_as_safe(self):
        # Even after both follow-up routes, the metal-shadow voxel stays in the
        # rescan list and keeps no fraction, never a silent clearance.
        rep = report(self.db, 'BAG-2')
        for result in rep['unmix_results']:
            positions = set(json.loads(result['rescan_positions_json']))
            self.assertIn('metal-shell', positions)
        v3 = next(v for v in rep['unmix_voxels'] if v['voxel_id'] == 'V3')
        self.assertIsNone(v3['r_fraction_lo'])
        self.assertIsNone(v3['r_fraction_hi'])
        # The bag interval never collapses to a zero-width "excluded" point.
        for result in rep['unmix_results']:
            self.assertGreater(result['r_fraction_hi'], result['r_fraction_lo'])

    def test_unexplained_residual_voxel_is_flagged_and_not_force_fit(self):
        # A reading outside even the calibration error box cannot be assigned a
        # fraction; it is recorded as unexplained and put on the rescan list.
        events = [
            {'event_id': 'U1', 'kind': 'screen_profile', 'bag_id': 'BAG-3',
             'profile_id': 'r', 'material': 'R', 'channels': R, 'revision': 1},
            {'event_id': 'U2', 'kind': 'screen_profile', 'bag_id': 'BAG-3',
             'profile_id': 'p', 'material': 'P', 'channels': P, 'revision': 1},
            {'event_id': 'U3', 'kind': 'screen_profile', 'bag_id': 'BAG-3',
             'profile_id': 'c', 'material': 'C', 'channels': C, 'revision': 1},
            {'event_id': 'U4', 'kind': 'screen_scan', 'bag_id': 'BAG-3',
             'scan_id': 'XU', 'channels': [5.0, 5.0, 3.0],
             'seen_at': '2026-11-22T08:10:00Z'},
            {'event_id': 'U5', 'kind': 'unmix_batch', 'bag_id': 'BAG-3',
             'batch_id': 'BATCH-U', 'scan_id': 'XU', 'reference_revision': 1,
             'restricted_material': 'R',
             'calibration': {'calibration_id': 'CAL-U',
                             'calibrated_at': '2026-11-22T07:55:00Z',
                             'bias_limit': 0.01, 'thickness_relative': 0.01},
             'voxels': [{'voxel_id': 'VU', 'position': 'core',
                         'channels': [5.0, 5.0, 3.0], 'thickness': 1.0,
                         'volume': 1.0, 'occluded': False,
                         'inspectable': True}]}]
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'u.sqlite'
            import_file(db, 'bag',
                        write_events(temp, 'bag3.json',
                                     [booking_event('BAG-3', 'BK-3')]))
            path = write_events(temp, 'unexplained.json', events)
            result = import_file(db, 'screen', path)['results'][-1]
            self.assertEqual(result['rescan_positions'], ['core'])
            rep = report(db, 'BAG-3')
            voxel = rep['unmix_voxels'][0]
            self.assertEqual(voxel['state'], 'unexplained_pending')
            self.assertIsNone(voxel['r_fraction_lo'])
            self.assertGreater(voxel['residual'], 0.0)
            # No fraction evidence means the whole bag spans [0, 1].
            bag_result = rep['unmix_results'][0]
            self.assertEqual((bag_result['r_fraction_lo'],
                              bag_result['r_fraction_hi']), (0.0, 1.0))
            self.assertEqual(audit(db)['status'], 'ok')


class ProvisionalLabelLibraryTests(unittest.TestCase):
    def test_x1_label_changes_with_reference_library(self):
        with tempfile.TemporaryDirectory() as temp:
            # Library with water at [4,6,2]: X1=[5,6,2] is distance 1 from water.
            db = Path(temp) / 'water.sqlite'
            import_file(db, 'bag', FIX / 'bag-ok.json')
            import_file(db, 'screen', FIX / 'screen-ok.json')
            rep = report(db, 'BAG-1')
            label = rep['screen_labels'][0]
            self.assertEqual((label['material'], label['state']), ('water', 'provisional'))
            self.assertAlmostEqual(label['distance'], 1.0, places=9)
            self.assertEqual(rep['screen_scans'][0]['state'], 'captured')
            self.assertEqual(audit(db)['status'], 'ok')

        with tempfile.TemporaryDirectory() as temp:
            # Fresh library registering only gel=[7,2,5]: the same raw X1 now
            # labels gel, proving the label is library-dependent not intrinsic.
            db = Path(temp) / 'gel.sqlite'
            import_file(db, 'bag',
                        write_events(temp, 'bag.json', [booking_event('BAG-1', 'BK-1')]))
            gel_events = [
                {'event_id': 'G1', 'kind': 'screen_profile', 'bag_id': 'BAG-1',
                 'profile_id': 'gel', 'material': 'gel', 'channels': [7, 2, 5],
                 'revision': 1},
                {'event_id': 'G2', 'kind': 'screen_scan', 'bag_id': 'BAG-1',
                 'scan_id': 'X1', 'channels': [5, 6, 2],
                 'seen_at': '2026-11-22T08:10:00Z'},
                {'event_id': 'G3', 'kind': 'screen_label', 'bag_id': 'BAG-1',
                 'label_id': 'L1', 'scan_id': 'X1'},
            ]
            import_file(db, 'screen', write_events(temp, 'gel.json', gel_events))
            label = report(db, 'BAG-1')['screen_labels'][0]
            self.assertEqual(label['material'], 'gel')
            self.assertEqual(label['state'], 'provisional')
            self.assertGreater(label['distance'], 1.0)


class SwabAndManualOpenRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'bag.sqlite'
        import_file(self.db, 'bag',
                    write_events(self.tmp.name, 'bag.json', [booking_event()]))
        import_file(self.db, 'screen', FIX / 'screen-mixture.json')

    def _swab_event(self, detected=False, actors=None):
        return {'event_id': 'SW-1', 'kind': 'unmix_swab', 'bag_id': 'BAG-2',
                'batch_id': 'BATCH-1', 'followup_id': 'RB-DET',
                'pocket_position': 'outer-pocket', 'seal_id': 'SEAL-2',
                'passenger_present': True,
                'actors': actors or ['officer-A', 'officer-B'],
                'r_detected': detected}

    def test_positive_swab_requires_manual_review_of_that_pocket_only(self):
        path = write_events(self.tmp.name, 'swab.json', [self._swab_event(True)])
        result = import_file(self.db, 'screen', path)['results'][0]
        self.assertEqual(result['status'], 'manual_review')
        self.assertEqual(result['manual_open_positions'], ['outer-pocket'])
        rep = report(self.db, 'BAG-2')
        # The inner interval must remain untouched.
        latest = rep['unmix_results'][-1]
        self.assertEqual(latest['state'], 'manual_review')
        self.assertIn('outer-pocket', json.loads(latest['rescan_positions_json']))

    def test_swab_requires_two_distinct_recorders(self):
        path = write_events(self.tmp.name, 'swab.json',
                            [self._swab_event(actors=['officer-A', 'officer-A'])])
        with self.assertRaisesRegex(ValueError, 'dual_record_required'):
            import_file(self.db, 'screen', path)

    def test_manual_open_publishes_measured_fraction_alongside_prior_versions(self):
        event = {'event_id': 'MO-1', 'kind': 'manual_open', 'bag_id': 'BAG-2',
                 'batch_id': 'BATCH-1', 'open_id': 'OPEN-1',
                 'at': '2026-11-22T10:00:00Z',
                 'actors': ['officer-A', 'officer-B'],
                 'findings': [
                     {'position': 'core', 'volume': 1.0, 'r_volume': 0.0},
                     {'position': 'outer-pocket', 'volume': 1.0, 'r_volume': 0.25},
                     {'position': 'metal-shell', 'volume': 2.0, 'r_volume': 0.75}]}
        path = write_events(self.tmp.name, 'open.json', [event])
        result = import_file(self.db, 'screen', path)['results'][0]
        measured = (0.25 + 0.75) / 4.0
        self.assertEqual(result['status'], 'opened_measured')
        self.assertAlmostEqual(result['r_fraction_range'][0], measured, places=9)
        rep = report(self.db, 'BAG-2')
        routes = [r['route'] for r in rep['unmix_results']]
        self.assertIn('manual_open', routes)
        # Prior provisional versions are still on the ledger.
        self.assertEqual(len(rep['unmix_results']), 4)
        self.assertEqual(len(rep['manual_opens']), 1)


class CalibrationReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'bag.sqlite'
        import_file(self.db, 'bag',
                    write_events(self.tmp.name, 'bag.json', [booking_event()]))
        import_file(self.db, 'screen', FIX / 'screen-mixture.json')

    def test_old_rescan_keeps_its_calibration_version_after_new_device_release(self):
        rep = report(self.db, 'BAG-2')
        followup = rep['unmix_followups'][0]
        self.assertEqual(followup['route'], 'A_fourth_channel')
        self.assertEqual(followup['calibration_version'], '4CH-v1')
        evidence = json.loads(followup['evidence_json'])
        # The calibration snapshot at capture time is embedded in the evidence.
        self.assertEqual(evidence['calibration_at_capture']['calibration_id'], 'CAL-1')

        # A second rescan claiming a newer calibration cannot overwrite the first.
        event = {'event_id': 'RA-2', 'kind': 'unmix_rescan', 'bag_id': 'BAG-2',
                 'batch_id': 'BATCH-1', 'followup_id': 'RA-2',
                 'device_id': 'SCAN4-2', 'calibration_version': '4CH-v2',
                 'restricted_material': 'R',
                 'reference_channel4': {'R': 3.0, 'P': 8.0, 'C': 5.0},
                 'residual_tolerance': 1e-6,
                 'voxel_channel4': {'V1': 5.0, 'V2': 6.75}}
        path = write_events(self.tmp.name, 'rescan2.json', [event])
        import_file(self.db, 'screen', path)
        rep = report(self.db, 'BAG-2')
        rescan_versions = [(f['followup_id'], f['calibration_version'])
                           for f in rep['unmix_followups'] if f['route'] == 'A_fourth_channel']
        self.assertEqual(rescan_versions, [('RA-1', '4CH-v1'), ('RA-2', '4CH-v2')])
        # Byte-level audit replay reproduces both decisions under their own
        # recorded calibration; the new calibration never rewrites RA-1.
        self.assertEqual(audit(self.db)['status'], 'ok')


class AuditTamperTests(unittest.TestCase):
    def test_tampering_with_a_projection_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'bag.sqlite'
            import_file(db, 'bag',
                        write_events(temp, 'bag.json', [booking_event()]))
            import_file(db, 'screen', FIX / 'screen-mixture.json')
            self.assertEqual(audit(db)['status'], 'ok')
            with connect(db) as writable:
                writable.execute(
                    "UPDATE unmix_bag_result SET r_fraction_hi=0.0 WHERE route='three_channel'")
                writable.commit()
            self.assertEqual(audit(db)['status'], 'divergent')

    def test_tampering_with_voxel_evidence_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'bag.sqlite'
            import_file(db, 'bag',
                        write_events(temp, 'bag.json', [booking_event()]))
            import_file(db, 'screen', FIX / 'screen-mixture.json')
            with connect(db) as writable:
                writable.execute(
                    "UPDATE unmix_voxel SET state='bounded' WHERE voxel_id='V3'")
                writable.commit()
            self.assertEqual(audit(db)['status'], 'divergent')

    def test_source_byte_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'bag.sqlite'
            import_file(db, 'bag',
                        write_events(temp, 'bag.json', [booking_event()]))
            import_file(db, 'screen', FIX / 'screen-mixture.json')
            with connect(db) as writable:
                row = writable.execute(
                    'SELECT sha,raw FROM source_file ORDER BY rowid LIMIT 1').fetchone()
                tampered = bytes(row['raw']).replace(b'clothes', b'crystal')
                writable.execute('UPDATE source_file SET raw=? WHERE sha=?',
                                 (tampered, row['sha']))
                writable.commit()
            self.assertEqual(audit(db)['status'], 'source_corrupt')


if __name__ == '__main__':
    unittest.main()
