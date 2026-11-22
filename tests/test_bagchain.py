import json
import subprocess
import sys
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory

from bagchain.audit import audit
from bagchain.report import report
from bagchain.source import import_file
from bagchain.store import connect

FIX=Path(__file__).resolve().parent.parent/'fixtures'

class BagchainTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'bag.sqlite'

    def put(self,kind,name):return import_file(self.db,kind,FIX/name)

    def event(self,kind,name,event):
        file=Path(self.tmp.name)/name
        file.write_text(json.dumps({'events':[event]}),encoding='utf-8')
        return import_file(self.db,kind,file)

    def normal(self):
        self.put('bag','bag-ok.json');self.put('sort','sort-ok.json');self.put('claim','claim-ok.json')

    def test_normal_three_business_subsystems_and_replay(self):
        print(__import__('bagchain').__file__)
        self.normal();r=report(self.db,'BAG-1')
        self.assertEqual(r['bag']['status'],'accepted')
        self.assertEqual(r['handoffs'][0]['status'],'accepted')
        self.assertEqual(r['claims'][0]['status'],'settled')
        self.assertEqual(audit(self.db)['status'],'ok')

    def test_tag_prohibited_item_and_weight_hold(self):
        self.put('bag','bag-bad.json');r=report(self.db,'BAG-BAD')
        self.assertEqual(r['bag']['status'],'held')
        self.assertEqual(r['decisions'][0]['reason'],'prohibited_item')

    def test_wrong_flight_and_custody_gap(self):
        self.put('bag','bag-ok.json');self.put('sort','sort-bad.json')
        r=report(self.db,'BAG-1')
        self.assertEqual(r['scans'][0]['reason'],'wrong_flight')
        self.assertEqual(r['handoffs'][0]['reason'],'missing_origin_scan')

    def test_wrong_claimant_rejected(self):
        self.normal();self.put('claim','claim-bad.json')
        self.assertEqual(report(self.db,'BAG-1')['claims'][-1]['reason'],'claimant_not_authorized')

    def test_cancellation_disputes_handoff_and_completed_pickup(self):
        self.normal()
        e={'event_id':'E6','kind':'booking_change','bag_id':'BAG-1','version':2,'action':'cancel'}
        self.event('bag','cancel.json',e)
        r=report(self.db,'BAG-1')
        self.assertEqual(r['booking']['status'],'cancelled')
        self.assertEqual(r['handoffs'][0]['status'],'disputed')
        self.assertEqual(r['arrivals'][0]['status'],'disputed')
        self.assertEqual(r['claims'][0]['status'],'disputed')
        self.assertEqual(audit(self.db)['status'],'ok')

    def test_reroute_rejudges_old_destination(self):
        self.normal()
        self.event('bag','reroute.json',{'event_id':'E6','kind':'booking_change','bag_id':'BAG-1',
                  'version':2,'action':'reroute','destination':'NEW'})
        r=report(self.db,'BAG-1')
        self.assertEqual(r['booking']['destination'],'NEW')
        self.assertEqual(r['handoffs'][0]['status'],'disputed')
        self.assertEqual(r['claims'][0]['status'],'disputed')

    def test_booking_version_conflict_rolls_back_file(self):
        self.put('bag','bag-ok.json')
        with self.assertRaisesRegex(ValueError,'booking_version_conflict'):
            self.event('bag','badversion.json',{'event_id':'E6','kind':'booking_change','bag_id':'BAG-1',
                      'version':4,'action':'cancel'})
        with connect(self.db) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM evidence WHERE event_id='E6'").fetchone())

    def test_event_id_conflict_does_not_overwrite(self):
        self.put('bag','bag-ok.json')
        with self.assertRaisesRegex(ValueError,'event_id_conflict'):
            self.event('bag','conflict.json',{'event_id':'E1','kind':'booking','bag_id':'BAG-NEW',
                      'booking_id':'BK-NEW','passenger':'P','flight':'F','destination':'X',
                      'checked_at':'2026-11-22T08:00:00Z','promised_at':'2026-11-22T09:00:00Z','items':[],'weight_kg':4})
        self.assertEqual(report(self.db,'BAG-1')['booking']['destination'],'DEST')

    def test_repeat_file_is_idempotent(self):
        self.put('bag','bag-ok.json');self.put('bag','bag-ok.json')
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],1)

    def test_late_claim_requires_transport_evidence(self):
        self.put('bag','bag-ok.json')
        e={'event_id':'L1','kind':'claim','bag_id':'BAG-1','claim_id':'CL1','claim_type':'late',
           'claimant':'P-1','at':'2026-11-22T09:30:00Z'}
        self.event('claim','late.json',e)
        self.assertEqual(report(self.db,'BAG-1')['claims'][0]['status'],'held')
        self.event('sort','scan.json',{'event_id':'S1','kind':'scan','bag_id':'BAG-1','scan_id':'SCAN1',
                   'station':'ORIGIN','flight':'FL100','seen_at':'2026-11-22T08:10:00Z','actor':'sorter'})
        # Existing claim remains historical until explicit re-evaluation trigger.
        self.assertEqual(report(self.db,'BAG-1')['claims'][0]['status'],'held')

    def test_damage_claim_needs_independent_proof_and_cap(self):
        self.put('bag','bag-ok.json');self.put('sort','sort-ok.json')
        base={'kind':'claim','bag_id':'BAG-1','claim_type':'damage','claimant':'P-1',
              'at':'2026-11-22T09:30:00Z','amount_cents':10000}
        self.event('claim','damage1.json',{**base,'event_id':'D1','claim_id':'CD1'})
        self.event('claim','damage2.json',{**base,'event_id':'D2','claim_id':'CD2',
                  'independent_witness':'auditor','damage_proof':'photo-sha'})
        r=report(self.db,'BAG-1')['claims']
        self.assertEqual([x['status'] for x in r],['review','compensable'])

    def test_late_arrival_converts_trace_to_capped_compensation(self):
        self.put('bag','bag-ok.json')
        self.event('sort','scan-first.json',{'event_id':'S1','kind':'scan','bag_id':'BAG-1','scan_id':'S1',
                   'station':'ORIGIN','flight':'FL100','seen_at':'2026-11-22T08:10:00Z','actor':'sorter'})
        self.event('claim','late-claim.json',{'event_id':'L1','kind':'claim','bag_id':'BAG-1','claim_id':'L1',
                   'claim_type':'late','claimant':'P-1','at':'2026-11-22T09:30:00Z','amount_cents':10000})
        self.assertEqual(report(self.db,'BAG-1')['claims'][0]['status'],'trace')
        self.event('sort','load-late.json',{'event_id':'LD1','kind':'load','bag_id':'BAG-1','load_id':'LD1',
                   'flight':'FL100','position':'BIN-2','confirmed_at':'2026-11-22T09:32:00Z','loader':'loader'})
        self.event('sort','handoff-late.json',{'event_id':'H1','kind':'handoff','bag_id':'BAG-1','handoff_id':'H1',
                   'from_station':'ORIGIN','to_station':'DEST','at':'2026-11-22T09:35:00Z',
                   'from_actor':'sorter','to_actor':'handler'})
        self.event('sort','arrival-late.json',{'event_id':'A1','kind':'arrival','bag_id':'BAG-1','arrival_id':'A1',
                   'airport':'DEST','at':'2026-11-22T10:00:00Z','handler':'handler'})
        self.assertEqual(report(self.db,'BAG-1')['claims'][0]['status'],'compensable')
        self.assertEqual(audit(self.db)['status'],'ok')

    def test_ticket_tag_and_flight_destination_are_distinct_decisions(self):
        base=json.loads((FIX/'bag-ok.json').read_text(encoding='utf-8'))['events'][0]
        for name,extra,reason in [('tag-person',{'tag_passenger':'OTHER'},'ticket_tag_passenger_mismatch'),
                                  ('tag-dest',{'tag_destination':'OTHER'},'ticket_tag_destination_mismatch'),
                                  ('flight-dest',{'flight_destination':'OTHER'},'flight_destination_mismatch')]:
            db=Path(self.tmp.name)/(name+'.sqlite')
            event={**base,'event_id':name,'bag_id':name,'booking_id':name,**extra}
            file=Path(self.tmp.name)/(name+'.json');file.write_text(json.dumps({'events':[event]}),encoding='utf-8')
            import_file(db,'bag',file)
            self.assertEqual(report(db,name)['decisions'][0]['reason'],reason)

    def test_cli_demo_report_audit(self):
        def run(*args):
            p=subprocess.run([sys.executable,'-m','bagchain',*args,'--db',str(self.db)],
                             capture_output=True,text=True,cwd=FIX.parent)
            self.assertEqual(p.returncode,0,p.stderr)
            return json.loads(p.stdout)
        self.assertEqual(run('demo')['claims'][0]['status'],'settled')
        self.assertEqual(run('audit')['status'],'ok')
        self.assertEqual(run('report','--bag','BAG-1')['booking']['destination'],'DEST')

    def test_public_cli_failure_fixtures_change_business_results(self):
        def run(command,file=None,bag=None):
            args=[sys.executable,'-m','bagchain',command]
            if file:args.append(str(FIX/file))
            if bag:args.extend(['--bag',bag])
            args.extend(['--db',str(self.db)])
            p=subprocess.run(args,capture_output=True,text=True,cwd=FIX.parent)
            self.assertEqual(p.returncode,0,p.stderr)
            return json.loads(p.stdout)
        run('bag-import','bag-ok.json')
        run('sort-import','sort-bad.json')
        run('claim-import','claim-bad.json')
        run('bag-import','bag-bad.json')
        good=run('report',bag='BAG-1');bad=run('report',bag='BAG-BAD')
        self.assertEqual(good['scans'][0]['status'],'held')
        self.assertEqual(good['claims'][0]['status'],'held')
        self.assertEqual(bad['bag']['status'],'held')
        self.assertEqual(run('audit')['status'],'ok')

if __name__=='__main__':unittest.main()
