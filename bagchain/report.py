"""Evidence and observable conclusions for a baggage tag."""
from .store import all_rows,connect,one

def report(db_path,bag):
    with connect(db_path) as db:
        tag=one(db,'SELECT * FROM bag_tag WHERE bag_id=?',(bag,))
        if not tag:raise ValueError('unknown_bag')
        book=one(db,'SELECT * FROM booking WHERE booking_id=?',(tag['booking_id'],))
        return {'bag':tag,'booking':book,
                'scans':all_rows(db,'SELECT * FROM scan WHERE bag_id=? ORDER BY seen_at,scan_id',(bag,)),
                'loads':all_rows(db,'SELECT * FROM load_manifest WHERE bag_id=? ORDER BY confirmed_at,load_id',(bag,)),
                'handoffs':all_rows(db,'SELECT * FROM handoff WHERE bag_id=? ORDER BY at,handoff_id',(bag,)),
                'arrivals':all_rows(db,'SELECT * FROM arrival WHERE bag_id=? ORDER BY at,arrival_id',(bag,)),
                'claims':all_rows(db,'SELECT * FROM claim WHERE bag_id=? ORDER BY at,claim_id',(bag,)),
                'settlements':all_rows(db,'SELECT s.* FROM settlement s JOIN claim c ON c.claim_id=s.claim_id WHERE c.bag_id=? ORDER BY s.payment_id',(bag,)),
                'decisions':all_rows(db,'SELECT * FROM decision WHERE bag_id=? ORDER BY id',(bag,)),
                'sources':all_rows(db,'SELECT DISTINCT source_sha FROM evidence WHERE bag_id=?',(bag,))}
