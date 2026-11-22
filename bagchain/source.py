"""Raw file import with event idempotence and atomic business projection."""
import json
from pathlib import Path
from .store import canonical,connect,digest,one

HANDLERS={
    'booking':('baggage','booking'),
    'booking_change':('baggage','change'),
    'scan':('sortation','scan'),
    'load':('sortation','load'),
    'handoff':('sortation','handoff'),
    'arrival':('sortation','arrival'),
    'claim':('claims','claim'),
    'locker_unit':('locker','unit'),
    'bag_silhouette':('locker','silhouette'),
    'locker_offer':('storage','offer'),
}

def import_file(db_path,kind,file):
    if kind not in ('bag','sort','claim','locker'):raise ValueError('unknown_import_kind')
    raw=Path(file).read_bytes();doc=json.loads(raw)
    if not isinstance(doc.get('events'),list) or not doc['events']:raise ValueError('events_required')
    sha=digest(raw);results=[]
    with connect(db_path) as db, db:
        old=one(db,'SELECT kind FROM source_file WHERE sha=?',(sha,))
        if old:
            if old['kind']!=kind:raise ValueError('source_kind_conflict')
        else:db.execute('INSERT INTO source_file VALUES(?,?,?)',(sha,kind,raw))
        for e in doc['events']:
            event_type=e['kind'];module_name,fn_name=HANDLERS[event_type]
            if {'baggage':'bag','sortation':'sort','claims':'claim','locker':'locker','storage':'locker'}[module_name]!=kind:
                raise ValueError('event_in_wrong_source')
            if not e.get('event_id') or not e.get('bag_id'):raise ValueError('event_identity_required')
            payload=canonical(e);old=one(db,'SELECT payload FROM evidence WHERE event_id=?',(e['event_id'],))
            if old:
                if old['payload']!=payload:raise ValueError('event_id_conflict')
                results.append({'event_id':e['event_id'],'status':'replayed'});continue
            seq=db.execute('SELECT COALESCE(MAX(received_seq),0)+1 FROM evidence').fetchone()[0]
            db.execute('INSERT INTO evidence VALUES(?,?,?,?,?,?)',(e['event_id'],event_type,e['bag_id'],sha,payload,seq))
            if module_name=='baggage':from . import baggage as module
            elif module_name=='sortation':from . import sortation as module
            elif module_name=='claims':from . import claims as module
            elif module_name=='locker':from . import locker as module
            else:from . import storage as module
            result=getattr(module,fn_name)(db,e,seq)
            results.append({'event_id':e['event_id'],**result})
    return {'source_sha':sha,'results':results}
