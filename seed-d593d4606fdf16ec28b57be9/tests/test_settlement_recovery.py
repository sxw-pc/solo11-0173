"""Acceptance tests for the recoverable airline/insurance clearing protocol.

Covered acceptance points:
1. The original fixtures still trace first, then turn compensable on late arrival.
2. The original one-shot ``pay`` still reproduces the bank/local divergence
   (bank 40000 cents vs local 20000 cents) after a mid-transfer crash.
3. Both pre-authorized routes clear the same claim: airline fronts or
   insurance fronts, producing different customer payers and different
   inter-party recovery debts.
4. After every failure point the customer is paid at most once; an unknown
   bank terminal keeps the share occupied (no renumber, no route switch).
5. An on-time arrival reversing the claim keeps the real bank facts, stops
   the unpaid remainder, and leaves a clawback todo; demo/audit still pass.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bagchain.audit import audit
from bagchain.report import report
from bagchain.settlement import (bank_finalize, bank_query, bank_snapshot, pay,
                                 settle, settle_sync)
from bagchain.source import import_file

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
ROOT = FIX.parent


class ClearingRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'case.sqlite'
        self.bank = self.root / 'bank.json'

    # -- scenario helpers ---------------------------------------------------
    def make_compensable(self):
        import_file(self.db, 'bag', FIX / 'bag-ok.json')
        import_file(self.db, 'sort', FIX / 'sort-before-arrival.json')
        traced = import_file(self.db, 'claim', FIX / 'late-claim.json')
        assert traced['results'][0]['status'] == 'trace'
        import_file(self.db, 'sort', FIX / 'late-arrival.json')
        assert report(self.db, 'BAG-1')['claims'][0]['status'] == 'compensable'

    def sign_shares(self):
        import_file(self.db, 'claim', FIX / 'airline-share.json')
        import_file(self.db, 'claim', FIX / 'insurance-share.json')

    def clearing(self):
        return report(self.db, 'BAG-1', bank_path=self.bank)['clearing']

    def write_request(self, name, **fields):
        file = self.root / name
        file.write_text(json.dumps(fields), encoding='utf-8')
        return file

    # -- acceptance 1: original fixtures trace, then compensable ------------
    def test_original_fixtures_trace_then_compensable(self):
        import_file(self.db, 'bag', FIX / 'bag-ok.json')
        import_file(self.db, 'sort', FIX / 'sort-before-arrival.json')
        result = import_file(self.db, 'claim', FIX / 'late-claim.json')
        self.assertEqual(result['results'][0]['status'], 'trace')
        self.assertEqual(report(self.db, 'BAG-1')['claims'][0]['status'], 'trace')
        import_file(self.db, 'sort', FIX / 'late-arrival.json')
        claim = report(self.db, 'BAG-1')['claims'][0]
        self.assertEqual(claim['status'], 'compensable')
        self.assertEqual(claim['reason'], 'late_arrival_confirmed')

    # -- acceptance 2: original pay keeps the reproducible divergence -------
    def test_original_pay_crash_reproduces_bank_local_divergence(self):
        self.make_compensable()
        with self.assertRaises(RuntimeError):
            pay(self.db, self.bank, FIX / 'payment-ok.json', fail_after_bank=True)
        bank = bank_snapshot(self.bank)
        self.assertEqual([(x['payment_id'], x['status'], x['amount_cents']) for x in bank],
                         [('PAY-1', 'transferred', 20000)])
        self.assertEqual(report(self.db, 'BAG-1')['settlements'], [])
        retry = self.write_request('payment-2.json', payment_id='PAY-2', claim_id='C-L',
                                   payer='airline-A', amount_cents=20000)
        self.assertEqual(pay(self.db, self.bank, retry)['status'], 'paid')
        bank = bank_snapshot(self.bank)
        self.assertEqual(sum(r['amount_cents'] for r in bank), 40000)
        self.assertEqual({r['payment_id'] for r in bank}, {'PAY-1', 'PAY-2'})
        settlements = report(self.db, 'BAG-1')['settlements']
        self.assertEqual(sum(s['amount_cents'] for s in settlements), 20000)
        # The bank ledger is the truth about customer cash: 40000 left the bank.
        self.assertEqual(self.clearing()['customer_received_cents'], 40000)
        anomalies = audit(self.db, bank_path=self.bank)['bank_anomalies']
        kinds = {a['kind'] for a in anomalies}
        self.assertIn('bank_transferred_without_settlement', kinds)
        self.assertIn('claim_overpaid', kinds)

    # -- acceptance 3: two authorized routes, two different outcomes --------
    def test_airline_first_route(self):
        self.make_compensable()
        self.sign_shares()
        # Recovery cannot start before the principal actually landed.
        with self.assertRaisesRegex(ValueError, 'no_open_debt'):
            settle(self.db, self.bank, FIX / 'recovery-insurance-to-airline.json')
        result = settle(self.db, self.bank, FIX / 'payment-airline-first.json')
        self.assertEqual(result['status'], 'sent')  # bank accepted, not terminal
        view = self.clearing()
        self.assertEqual(view['customer_received_cents'], 0)  # acceptance is not cash
        self.assertEqual([u['payment_id'] for u in view['bank_unknown']], ['PAY-A1'])
        bank_finalize(self.bank, 'PAY-A1', 'transferred')
        synced = settle_sync(self.db, self.bank)['synced']
        self.assertEqual([(t['payment_id'], t['to']) for t in synced], [('PAY-A1', 'settled')])
        view = self.clearing()
        self.assertEqual(view['customer_received'],
                         [{'payment_id': 'PAY-A1', 'payer': 'airline-A',
                           'payee': 'P-1', 'amount_cents': 20000}])
        self.assertEqual(view['party_debts'],
                         [{'debt_id': 'DEBT:C-L:insurance-I->airline-A', 'claim_id': 'C-L',
                           'debtor': 'insurance-I', 'creditor': 'airline-A',
                           'amount_cents': 8000, 'status': 'open',
                           'source': 'fronted:PAY-A1'}])
        self.assertIn({'todo': 'collect_party_share', 'debt_id': 'DEBT:C-L:insurance-I->airline-A',
                       'debtor': 'insurance-I', 'creditor': 'airline-A', 'amount_cents': 8000},
                      view['todos'])
        # The external bank snapshot fixture agrees with the live bank.
        self.assertEqual(audit(self.db, bank_path=FIX / 'bank-snapshot.json')['status'], 'ok')
        # Insurance now pays its 8000 share to the fronter — a separate debt.
        self.assertEqual(settle(self.db, self.bank, FIX / 'recovery-insurance-to-airline.json')['status'],
                         'sent')
        bank_finalize(self.bank, 'PAY-R-IA', 'transferred')
        settle_sync(self.db, self.bank)
        view = self.clearing()
        self.assertEqual(view['customer_received_cents'], 20000)  # recovery is not customer cash
        self.assertEqual(view['party_debts'][0]['status'], 'settled')
        self.assertEqual(view['todos'], [])
        self.assertEqual(audit(self.db, bank_path=self.bank)['status'], 'ok')

    def test_insurance_first_route(self):
        self.make_compensable()
        self.sign_shares()
        settle(self.db, self.bank, FIX / 'payment-insurance-first.json')
        bank_finalize(self.bank, 'PAY-I1', 'transferred')
        settle_sync(self.db, self.bank)
        view = self.clearing()
        self.assertEqual(view['customer_received'],
                         [{'payment_id': 'PAY-I1', 'payer': 'insurance-I',
                           'payee': 'P-1', 'amount_cents': 20000}])
        self.assertEqual([(d['debtor'], d['creditor'], d['amount_cents'], d['status'])
                          for d in view['party_debts']],
                         [('airline-A', 'insurance-I', 12000, 'open')])
        settle(self.db, self.bank, FIX / 'recovery-airline-to-insurance.json')
        bank_finalize(self.bank, 'PAY-R-AI', 'transferred')
        settle_sync(self.db, self.bank)
        view = self.clearing()
        self.assertEqual(view['customer_received_cents'], 20000)
        self.assertEqual(view['party_debts'][0]['status'], 'settled')
        self.assertEqual(view['todos'], [])
        self.assertEqual(audit(self.db, bank_path=self.bank)['status'], 'ok')

    # -- acceptance 4: failure-point recovery -------------------------------
    def test_lost_response_after_accept_recovers_without_duplicate(self):
        self.make_compensable()
        self.sign_shares()
        with self.assertRaises(RuntimeError):
            settle(self.db, self.bank, FIX / 'payment-airline-first.json',
                   fail_after_bank=True)
        # Bank durably accepted; locally the request is only reserved.
        self.assertEqual(bank_query(self.bank, 'PAY-A1')['status'], 'accepted')
        # Retrying the same request file adopts the bank record, no duplicate.
        result = settle(self.db, self.bank, FIX / 'payment-airline-first.json')
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(len(bank_snapshot(self.bank)), 1)
        # The bank then transferred but the response was lost again: the
        # recovery sweep, anchored on the bank record, books exactly one receipt.
        bank_finalize(self.bank, 'PAY-A1', 'transferred')
        self.assertEqual(report(self.db, 'BAG-1')['settlements'], [])
        settle_sync(self.db, self.bank)
        settle_sync(self.db, self.bank)  # duplicate callback adds no cash
        settlements = report(self.db, 'BAG-1')['settlements']
        self.assertEqual([(s['payment_id'], s['amount_cents']) for s in settlements],
                         [('PAY-A1', 20000)])
        self.assertEqual(self.clearing()['customer_received_cents'], 20000)
        self.assertEqual(len(bank_snapshot(self.bank)), 1)

    def test_unknown_terminal_blocks_renumber_and_route_switch(self):
        self.make_compensable()
        self.sign_shares()
        settle(self.db, self.bank, FIX / 'payment-airline-first.json')
        # Same request identity with different content is rejected.
        tampered = self.write_request('tampered.json', payment_id='PAY-A1', claim_id='C-L',
                                      payer='airline-A', amount_cents=9999)
        with self.assertRaisesRegex(ValueError, 'request_id_conflict'):
            settle(self.db, self.bank, tampered)
        # Renumbering the same share is blocked while the outcome is unknown.
        renumbered = self.write_request('renumbered.json', payment_id='PAY-9', claim_id='C-L',
                                        payer='airline-A', amount_cents=20000)
        with self.assertRaisesRegex(ValueError, 'share_occupied'):
            settle(self.db, self.bank, renumbered)
        # Switching route to the other payer is equally blocked.
        with self.assertRaisesRegex(ValueError, 'share_occupied'):
            settle(self.db, self.bank, FIX / 'payment-insurance-first.json')
        view = self.clearing()
        self.assertEqual(view['customer_received_cents'], 0)
        self.assertEqual([(u['payment_id'], u['bank_status']) for u in view['bank_unknown']],
                         [('PAY-A1', 'accepted')])
        self.assertIn({'todo': 'confirm_bank_terminal', 'payment_id': 'PAY-A1',
                       'bank_status': 'accepted'}, view['todos'])
        # Even after the bank really transferred, the other party must not pay again.
        bank_finalize(self.bank, 'PAY-A1', 'transferred')
        with self.assertRaisesRegex(ValueError, 'share_occupied'):
            settle(self.db, self.bank, FIX / 'payment-insurance-first.json')
        settle_sync(self.db, self.bank)
        with self.assertRaisesRegex(ValueError, 'principal_already_settled'):
            settle(self.db, self.bank, FIX / 'payment-insurance-first.json')
        self.assertEqual(self.clearing()['customer_received_cents'], 20000)
        self.assertEqual(len(report(self.db, 'BAG-1')['settlements']), 1)

    def test_verified_failure_receipt_releases_occupation(self):
        self.make_compensable()
        self.sign_shares()
        settle(self.db, self.bank, FIX / 'payment-airline-first.json')
        bank_finalize(self.bank, 'PAY-A1', 'failed')  # bank attaches its receipt
        transitions = settle_sync(self.db, self.bank)['synced']
        self.assertEqual([(t['payment_id'], t['to']) for t in transitions], [('PAY-A1', 'failed')])
        view = self.clearing()
        self.assertEqual(view['bank_unknown'], [])
        self.assertEqual(view['customer_received_cents'], 0)
        # Occupation released: the other authorized route may now pay.
        settle(self.db, self.bank, FIX / 'payment-insurance-first.json')
        bank_finalize(self.bank, 'PAY-I1', 'transferred')
        settle_sync(self.db, self.bank)
        view = self.clearing()
        self.assertEqual(view['customer_received'],
                         [{'payment_id': 'PAY-I1', 'payer': 'insurance-I',
                           'payee': 'P-1', 'amount_cents': 20000}])
        self.assertEqual(audit(self.db, bank_path=self.bank)['status'], 'ok')

    def test_unverifiable_failure_receipt_keeps_occupation(self):
        self.make_compensable()
        self.sign_shares()
        settle(self.db, self.bank, FIX / 'payment-airline-first.json')
        forged = bank_snapshot(self.bank)
        forged[0]['status'] = 'failed'
        forged[0]['receipt'] = 'forged-receipt'
        self.bank.write_text(json.dumps(forged), encoding='utf-8')
        self.assertEqual(settle_sync(self.db, self.bank)['synced'], [])
        view = self.clearing()
        self.assertEqual([u['payment_id'] for u in view['bank_unknown']], ['PAY-A1'])
        with self.assertRaisesRegex(ValueError, 'share_occupied'):
            settle(self.db, self.bank, FIX / 'payment-insurance-first.json')
        self.assertEqual(audit(self.db, bank_path=self.bank)['bank_anomalies'],
                         [{'kind': 'bank_failure_receipt_invalid', 'payment_id': 'PAY-A1'}])

    def test_recovery_from_external_bank_snapshot(self):
        self.make_compensable()
        self.sign_shares()
        with self.assertRaises(RuntimeError):
            settle(self.db, self.bank, FIX / 'payment-airline-first.json',
                   fail_after_bank=True)
        # The local bank file is replaced by the external statement: the
        # terminal fact there is the recovery anchor for the local books.
        self.bank.write_text((FIX / 'bank-snapshot.json').read_text(encoding='utf-8'),
                             encoding='utf-8')
        transitions = settle_sync(self.db, self.bank)['synced']
        self.assertEqual([(t['payment_id'], t['from'], t['to']) for t in transitions],
                         [('PAY-A1', 'reserved', 'settled')])
        view = self.clearing()
        self.assertEqual(view['customer_received_cents'], 20000)
        self.assertEqual(view['party_debts'][0]['debtor'], 'insurance-I')
        self.assertEqual(audit(self.db, bank_path=self.bank)['status'], 'ok')

    # -- acceptance 5: reversal keeps bank facts, demo/audit still pass -----
    def test_ontime_arrival_reversal_keeps_bank_facts(self):
        self.make_compensable()
        self.sign_shares()
        settle(self.db, self.bank, FIX / 'payment-airline-first.json')
        bank_finalize(self.bank, 'PAY-A1', 'transferred')
        settle_sync(self.db, self.bank)
        self.assertEqual(self.clearing()['customer_received_cents'], 20000)
        # On-time arrival proof (09:00 <= promised) overturns the lateness decision.
        import_file(self.db, 'sort', FIX / 'ontime-arrival.json')
        claim = report(self.db, 'BAG-1')['claims'][0]
        self.assertEqual((claim['status'], claim['reason']), ('held', 'delivered_within_promise'))
        # The bank fact is not erased by the claim status change.
        self.assertEqual(bank_query(self.bank, 'PAY-A1')['status'], 'transferred')
        view = self.clearing()
        self.assertEqual(view['customer_received_cents'], 20000)
        self.assertIn({'todo': 'clawback_passenger', 'claim_id': 'C-L',
                       'from': 'P-1', 'amount_cents': 20000}, view['todos'])
        self.assertEqual(view['party_debts'][0]['status'], 'void')
        # Unpaid money stops: no new principal on either route, no legacy pay.
        with self.assertRaisesRegex(ValueError, 'claim_not_compensable'):
            settle(self.db, self.bank, FIX / 'payment-insurance-first.json')
        with self.assertRaisesRegex(ValueError, 'claim_not_compensable'):
            pay(self.db, self.bank, FIX / 'payment-ok.json')
        self.assertEqual(audit(self.db)['status'], 'ok')
        self.assertEqual(audit(self.db, bank_path=self.bank)['status'], 'ok')

    def test_reversal_stops_reserved_unsent_payment(self):
        self.make_compensable()
        self.sign_shares()
        with self.assertRaises(RuntimeError):
            settle(self.db, self.bank, FIX / 'payment-airline-first.json',
                   fail_after_bank=True)  # bank accepted, local only reserved
        import_file(self.db, 'sort', FIX / 'ontime-arrival.json')
        self.assertEqual(report(self.db, 'BAG-1')['claims'][0]['status'], 'held')
        # The withdrawn request cannot be executed anymore...
        with self.assertRaisesRegex(ValueError, 'claim_not_compensable'):
            settle(self.db, self.bank, FIX / 'payment-airline-first.json')
        # ...but if the bank still completes it, the bank fact is booked.
        bank_finalize(self.bank, 'PAY-A1', 'transferred')
        settle_sync(self.db, self.bank)
        view = self.clearing()
        self.assertEqual(view['customer_received_cents'], 20000)
        self.assertIn({'todo': 'clawback_passenger', 'claim_id': 'C-L',
                       'from': 'P-1', 'amount_cents': 20000}, view['todos'])

    def test_original_demo_and_audit_cli_still_pass(self):
        def run(*args):
            proc = subprocess.run([sys.executable, '-m', 'bagchain', *args],
                                  capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        db = str(self.db)
        self.assertEqual(run('demo', '--db', db)['claims'][0]['status'], 'settled')
        self.assertEqual(run('audit', '--db', db)['status'], 'ok')
        self.assertEqual(run('report', '--db', db, '--bag', 'BAG-1')['booking']['destination'],
                         'DEST')

    def test_clearing_flow_via_cli(self):
        def run(*args):
            proc = subprocess.run([sys.executable, '-m', 'bagchain', *args],
                                  capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        db, bank = str(self.db), str(self.bank)
        run('bag-import', str(FIX / 'bag-ok.json'), '--db', db)
        run('sort-import', str(FIX / 'sort-before-arrival.json'), '--db', db)
        run('claim-import', str(FIX / 'late-claim.json'), '--db', db)
        run('sort-import', str(FIX / 'late-arrival.json'), '--db', db)
        run('claim-import', str(FIX / 'airline-share.json'), '--db', db)
        run('claim-import', str(FIX / 'insurance-share.json'), '--db', db)
        self.assertEqual(run('settle-request', str(FIX / 'payment-airline-first.json'),
                             '--db', db, '--bank', bank)['status'], 'sent')
        run('bank-finalize', 'PAY-A1', '--bank', bank, '--outcome', 'transferred')
        self.assertEqual(run('settle-sync', '--db', db, '--bank', bank)['synced'][0]['to'],
                         'settled')
        view = run('report', '--db', db, '--bag', 'BAG-1', '--bank', bank)
        self.assertEqual(view['clearing']['customer_received_cents'], 20000)
        self.assertEqual(run('audit', '--db', db, '--bank', bank)['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
