"""Public CLI entry points for independent baggage, sortation and claim ledgers."""
import argparse
import json
from pathlib import Path
from .audit import audit
from .report import report
from .source import import_file
from .settlement import bank_finalize, pay, settle, settle_sync

def demo(db):
    fixtures=Path(__file__).resolve().parent.parent/'fixtures'
    for kind,name in [('bag','bag-ok.json'),('sort','sort-ok.json'),('claim','claim-ok.json')]:
        import_file(db,kind,fixtures/name)
    return report(db,'BAG-1')

def main():
    parser=argparse.ArgumentParser(description='Offline baggage evidence and custody ledger')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('bag-import','sort-import','claim-import'):
        cmd=sub.add_parser(name);cmd.add_argument('file');cmd.add_argument('--db',required=True)
    view=sub.add_parser('report');view.add_argument('--db',required=True);view.add_argument('--bag',required=True)
    view.add_argument('--bank')
    check=sub.add_parser('audit');check.add_argument('--db',required=True);check.add_argument('--bank')
    sample=sub.add_parser('demo');sample.add_argument('--db',required=True)
    payment=sub.add_parser('settle-pay');payment.add_argument('file');payment.add_argument('--db',required=True);payment.add_argument('--bank',required=True)
    request=sub.add_parser('settle-request');request.add_argument('file');request.add_argument('--db',required=True)
    request.add_argument('--bank',required=True);request.add_argument('--fail-after-bank',action='store_true')
    sync=sub.add_parser('settle-sync');sync.add_argument('--db',required=True);sync.add_argument('--bank',required=True)
    finalize=sub.add_parser('bank-finalize');finalize.add_argument('payment_id');finalize.add_argument('--bank',required=True)
    finalize.add_argument('--outcome',choices=['transferred','failed'],required=True)
    args=parser.parse_args()
    if args.command.endswith('-import'):result=import_file(args.db,args.command.split('-')[0],args.file)
    elif args.command=='report':result=report(args.db,args.bag,bank_path=args.bank)
    elif args.command=='audit':result=audit(args.db,bank_path=args.bank)
    elif args.command=='settle-pay':result=pay(args.db,args.bank,args.file)
    elif args.command=='settle-request':result=settle(args.db,args.bank,args.file,fail_after_bank=args.fail_after_bank)
    elif args.command=='settle-sync':result=settle_sync(args.db,args.bank)
    elif args.command=='bank-finalize':result=bank_finalize(args.bank,args.payment_id,args.outcome)
    else:result=demo(args.db)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
