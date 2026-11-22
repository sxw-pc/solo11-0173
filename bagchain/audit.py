"""Rebuild current decisions from retained original files."""
import tempfile
from pathlib import Path
from .source import import_file
from .store import all_rows,connect,digest

TABLES=('booking','bag_tag','scan','load_manifest','handoff','arrival','claim','decision','evidence')

def audit(db_path):
    with connect(db_path) as db:
        sources=[(r['kind'],bytes(r['raw']),r['sha']) for r in db.execute('SELECT kind,raw,sha FROM source_file ORDER BY rowid')]
        original={name:all_rows(db,f'SELECT * FROM {name} ORDER BY 1') for name in TABLES}
    with tempfile.TemporaryDirectory(prefix='bagchain-audit-') as folder:
        rebuilt=Path(folder)/'rebuilt.sqlite'
        for i,(kind,raw,sha) in enumerate(sources):
            if digest(raw)!=sha:return {'status':'source_corrupt','source_sha':sha}
            file=Path(folder)/f'{i}.json';file.write_bytes(raw)
            import_file(rebuilt,kind,file)
        with connect(rebuilt) as db:
            replay={name:all_rows(db,f'SELECT * FROM {name} ORDER BY 1') for name in TABLES}
    mismatches=[name for name in TABLES if original[name]!=replay[name]]
    return {'status':'ok' if not mismatches else 'divergent','source_count':len(sources),'mismatches':mismatches}
