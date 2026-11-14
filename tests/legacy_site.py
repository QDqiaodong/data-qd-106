"""Frozen pre-fix snapshot of the announcement collector.

This is the original TOCTOU-vulnerable implementation kept verbatim (only the
module docstring added).  Tests load it through importlib so the package never
imports it in production.  It exists solely to demonstrate, deterministically,
the three defects fixed in recall_collect.site:

1. a path replaced by an out-of-root symlink after ``resolve`` returns is read
   anyway, so out-of-root bytes get parsed;
2. a file growing beyond 1 MB between the ``stat`` size check and ``read_text``
   bypasses the byte cap;
3. detail pages are read a second time, so two reads of the same path can
   disagree (and the second, unchecked, read decides the detail verdict).
"""
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from datetime import date

class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links=[]; self.blocks=[]; self.capture=False; self.parts=[]
        self.feed(text); self.close()
        if self.capture:
            raise ValueError("unclosed JSON script")
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='a' and a.get('rel') in ('notice','next'):
            if not a.get('href'): raise ValueError('empty link')
            self.links.append((a['rel'],a['href']))
        if tag=='script' and a.get('type')=='application/json':
            self.capture=True; self.parts=[]
    def handle_data(self, data):
        if self.capture: self.parts.append(data)
    def handle_endtag(self, tag):
        if tag=='script' and self.capture:
            self.blocks.append(''.join(self.parts)); self.capture=False

def resolve(root, parent, href):
    u=urlsplit(href)
    if u.scheme or u.netloc or u.query or u.fragment:
        raise ValueError('only root-contained paths without URL components are accepted')
    path=(parent/unquote(u.path)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('source escapes root or is missing')
    if path.suffix!='.html' or path.stat().st_size>1_000_000:
        raise ValueError('source must be a small HTML page')
    return path

def notice(block):
    d=json.loads(block)
    if not isinstance(d,dict) or set(d)!={'recall_id','published','product','lots','reason'}:
        raise ValueError('invalid notice fields')
    for k in ('recall_id','published','product','reason'):
        if not isinstance(d[k],str) or not d[k].strip(): raise ValueError('empty field')
    if date.fromisoformat(d['published']).isoformat()!=d['published']:
        raise ValueError('published must be YYYY-MM-DD')
    lots=d['lots']
    if not isinstance(lots,list) or not lots or any(not isinstance(x,str) or not x.strip() for x in lots):
        raise ValueError('invalid lots')
    d['lots']=sorted(set(lots))
    return d

def crawl(source):
    root=Path(source).resolve()
    pending=[resolve(root,root,'index.html')]; seen=set(); records={}; links=set()
    while pending:
        path=pending.pop(0)
        if path in seen: continue
        seen.add(path)
        if len(seen)>1000: raise ValueError('page limit exceeded')
        page=Page(path.read_text(encoding='utf-8'))
        for rel,href in page.links:
            target=resolve(root,path.parent,href)
            pending.append(target)
            if rel=='notice': links.add(target)
        for block in page.blocks:
            d=notice(block); key=d['recall_id']
            if key in records and records[key]['record']!=d:
                raise ValueError('conflicting recall ID: '+key)
            row=records.setdefault(key,{'record':d,'sources':[]})
            row['sources'].append(path.relative_to(root).as_posix())
    # Every advertised detail must actually contain a record.
    for path in links:
        if not Page(path.read_text(encoding='utf-8')).blocks:
            raise ValueError('detail has no notice')
    if not records: raise ValueError('no notices')
    return records,len(seen)
