from contextlib import closing
import json
import sqlite3
from pathlib import Path
from .site import crawl

def collect(source, database):
    rows,pages=crawl(source)  # Validate the entire source before opening the target.
    target=Path(database); target.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(target)) as db, db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS recalls(recall_id TEXT PRIMARY KEY, published TEXT NOT NULL,
          product TEXT NOT NULL, lots_json TEXT NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(recall_id TEXT NOT NULL, path TEXT NOT NULL,
          PRIMARY KEY(recall_id,path));
        ''')
        before=db.execute('SELECT count(*) FROM recalls').fetchone()[0]
        for key,row in sorted(rows.items()):
            d=row['record']; values=(key,d['published'],d['product'],json.dumps(d['lots']),d['reason'])
            old=db.execute('SELECT * FROM recalls WHERE recall_id=?',(key,)).fetchone()
            if old is not None and old!=values:
                raise ValueError('stored recall differs: '+key)
            db.execute('INSERT OR IGNORE INTO recalls VALUES (?,?,?,?,?)',values)
            db.executemany('INSERT OR IGNORE INTO sources VALUES (?,?)',[(key,p) for p in row['sources']])
        total=db.execute('SELECT count(*) FROM recalls').fetchone()[0]
        return {'pages':pages,'source_unique_recalls':len(rows),'inserted':total-before,'stored':total}
