"""Public CLI entry points for independent baggage, sortation and claim ledgers."""
import argparse
import json
from pathlib import Path
from .audit import audit
from .report import report
from .source import import_file

def demo(db):
    fixtures=Path(__file__).resolve().parent.parent/'fixtures'
    for kind,name in [('bag','bag-ok.json'),('sort','sort-ok.json'),('claim','claim-ok.json')]:
        import_file(db,kind,fixtures/name)
    return report(db,'BAG-1')

def main():
    parser=argparse.ArgumentParser(description='Offline baggage evidence and custody ledger')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('bag-import','sort-import','claim-import','screen-import'):
        cmd=sub.add_parser(name);cmd.add_argument('file');cmd.add_argument('--db',required=True)
    view=sub.add_parser('report');view.add_argument('--db',required=True);view.add_argument('--bag',required=True)
    check=sub.add_parser('audit');check.add_argument('--db',required=True)
    sample=sub.add_parser('demo');sample.add_argument('--db',required=True)
    args=parser.parse_args()
    if args.command.endswith('-import'):result=import_file(args.db,args.command.split('-')[0],args.file)
    elif args.command=='report':result=report(args.db,args.bag)
    elif args.command=='audit':result=audit(args.db)
    else:result=demo(args.db)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
