import tempfile
import unittest
from pathlib import Path
from bagchain.source import import_file
from bagchain.report import report
from bagchain.audit import audit

ROOT=Path(__file__).resolve().parent.parent

class LockerSeedTests(unittest.TestCase):
    def test_front_projection_only_offers_small_unit(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/'bag.sqlite'
            import_file(db,'bag',ROOT/'fixtures/bag-ok.json')
            import_file(db,'locker',ROOT/'fixtures/locker-ok.json')
            self.assertEqual((report(db,'BAG-1')['locker_offers'][0]['unit_id'],report(db,'BAG-1')['locker_offers'][0]['status']),('S','provisional'))
            self.assertEqual(audit(db)['status'],'ok')

if __name__=='__main__':unittest.main()
