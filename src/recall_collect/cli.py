import argparse
import json
from .store import collect

def main():
    p=argparse.ArgumentParser(description='Collect a caller-owned offline synthetic notice site')
    p.add_argument('--source',required=True); p.add_argument('--database',required=True)
    a=p.parse_args()
    try: result=collect(a.source,a.database)
    except (ValueError,OSError) as e: p.exit(2,str(e)+'\n')
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
if __name__=='__main__': main()
