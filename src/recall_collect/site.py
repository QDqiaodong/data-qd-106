import json
import os
import stat
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from datetime import date

MAX_SIZE = 1_000_000
_READ_CHUNK = 1 << 16


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
    if path.suffix!='.html' or path.stat().st_size>MAX_SIZE:
        raise ValueError('source must be a small HTML page')
    return path


# Thin module-level seams so tests can inject deterministic synchronization
# points without threads, sleeps or repeated stat calls. Production callers
# never override them.
def _open_candidate(path):
    # O_NOFOLLOW (POSIX; unavailable on some platforms) makes a symlink that
    # replaced the resolved page fail instead of being followed.
    flags=os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    return os.open(path, flags)


def _read_capped(fd, limit=MAX_SIZE):
    """Read at most ``limit`` bytes from ``fd``; raise if more are available."""
    chunks=[]; total=0
    while True:
        chunk=os.read(fd, _READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError('source must be a small HTML page')
        chunks.append(chunk)
    return b''.join(chunks)


def _after_validate(root, path):
    """Synchronization seam used by tests to mutate the tree post-validation."""


def capture(root, path):
    """Capture the single authoritative text of one canonical page.

    The validation and the actual bytes are bound to one file descriptor, so a
    path that is replaced (by a symlink, by swapping a parent directory, or by
    growing past the limit) between the link resolution and parsing can never
    make the parsed content diverge from the validated object.
    """
    fd=_open_candidate(path)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError('source must be a small HTML page')
        # Test hook: fires after the descriptor-based checks, before any byte
        # is read, so injected replacements/growth race against this capture.
        _after_validate(root, path)
        data=_read_capped(fd, MAX_SIZE)
        # The cap is enforced on the bytes actually read, not on a stat size:
        # a file growing during the read is rejected above. Re-resolve the
        # path and require it to still name this same inode inside the root,
        # closing the window where a parent directory was swapped to an
        # out-of-root link.
        current=Path(os.path.realpath(path))
        if not current.is_relative_to(root) or not os.path.samefile(current, fd):
            raise ValueError('source escapes root or is missing')
    finally:
        os.close(fd)
    return data.decode('utf-8')


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
    captured={}
    while pending:
        path=pending.pop(0)
        if path in seen: continue
        seen.add(path)
        if len(seen)>1000: raise ValueError('page limit exceeded')
        # One capture per canonical page: every later check reuses this text.
        text=capture(root,path)
        captured[path]=text
        page=Page(text)
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
    # Every advertised detail must actually contain a record. Reuse the same
    # captured bytes; the page is never read a second time.
    for path in links:
        if not Page(captured[path]).blocks:
            raise ValueError('detail has no notice')
    if not records: raise ValueError('no notices')
    return records,len(seen)
