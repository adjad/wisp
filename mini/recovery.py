"""Offline incident recovery. Originals are evidence, never repair targets."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import secrets
import struct

from mini import backup
from mini.store import Store, StoreUnavailable, private_directory


def existing_private(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise StoreUnavailable('Unsafe recovery path')
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StoreUnavailable('Unsafe recovery path')
    return path


def state_inventory(path):
    path = existing_private(path)
    rows = {}
    for name in ('node.sqlite3','node.sqlite3-wal','node.sqlite3-shm','node.sqlite3-journal'):
        try:
            fd = os.open(path/name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        with os.fdopen(fd,'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 1024**3:
                raise StoreUnavailable('Unsafe recovery state')
            h = hashlib.sha256()
            for block in iter(lambda: source.read(1024*1024),b''): h.update(block)
            after = os.fstat(source.fileno())
            signature = lambda value: (value.st_dev,value.st_ino,value.st_mode,value.st_uid,value.st_nlink,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
            if signature(info) != signature(after) or signature((path/name).lstat()) != signature(after):
                raise StoreUnavailable('Recovery state changed')
            rows[name] = {'device':info.st_dev,'inode':info.st_ino,'size':info.st_size,
                          'mtime_ns':info.st_mtime_ns,'ctime_ns':info.st_ctime_ns,'sha256':h.hexdigest()}
    return rows


def inventory_digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',',':')).encode()).hexdigest()


def copy_approved(source, destination, expected):
    """Copy approved bytes from a pinned regular FD; never chmod a source link."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as reader:
        def check():
            info = os.fstat(reader.fileno())
            observed = {'device':info.st_dev, 'inode':info.st_ino, 'size':info.st_size,
                        'mtime_ns':info.st_mtime_ns, 'ctime_ns':info.st_ctime_ns}
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or observed != {k:v for k,v in expected.items() if k != 'sha256'}):
                raise StoreUnavailable('Recovery source changed')
        check()
        output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(output, 'wb') as writer:
            h = hashlib.sha256()
            remaining = expected['size']
            while remaining:
                block = reader.read(min(remaining, 1024*1024))
                if not block: raise StoreUnavailable('Recovery source changed')
                writer.write(block); h.update(block); remaining -= len(block)
            if reader.read(1) or h.hexdigest() != expected['sha256']:
                raise StoreUnavailable('Recovery source changed')
            check()
            writer.flush(); os.fsync(writer.fileno())


def inactive(path):
    result = subprocess.run(['/usr/sbin/lsof','-nP','-t','+D',str(path)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=15, env={'PATH':'/usr/bin:/bin','LC_ALL':'C'})
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def doctor(path):
    rows = state_inventory(path)
    main = rows.get('node.sqlite3')
    state = ('missing_database' if main is None else
             'zero_main_with_wal' if main['size'] == 0 and 'node.sqlite3-wal' in rows else
             'zero_main_without_wal' if main['size'] == 0 else 'existing_database')
    return {'schema_version':1,'state':state,'inventory':rows,'inventory_sha256':inventory_digest(rows),
            'automatic_repair':False,'recovery':'new_destination_only'}


def reconstruct_wal(raw):
    """Only a complete checksum-valid WAL covering every committed DB page.

    Format: https://www.sqlite.org/fileformat.html#wal_file_format . Partial,
    reset/trailing, or incomplete page sets are intentionally inconclusive.
    """
    if len(raw)<32:raise StoreUnavailable('Incomplete recovery WAL')
    magic,version,size,_,salt1,salt2,c0,c1=struct.unpack('>8I',raw[:32])
    if magic not in (0x377f0682,0x377f0683) or version!=3007000 or not 512<=size<=65536 or size&(size-1):
        raise StoreUnavailable('Unsupported recovery WAL')
    def checksum(data,state=(0,0)):
        words=struct.unpack(('<' if magic==0x377f0682 else '>')+str(len(data)//4)+'I',data)
        a,b=state
        for i in range(0,len(words),2):
            a=(a+words[i]+b)&0xffffffff;b=(b+words[i+1]+a)&0xffffffff
        return a,b
    state=checksum(raw[:24])
    if state!=(c0,c1) or (len(raw)-32)%(size+24):raise StoreUnavailable('Invalid recovery WAL')
    pages={};committed=None;count=0
    for offset in range(32,len(raw),size+24):
        header=raw[offset:offset+24];data=raw[offset+24:offset+24+size]
        number,count,s1,s2,c0,c1=struct.unpack('>6I',header)
        state=checksum(header[:8]+data,state)
        if not 0<number<=1024**3//size or (s1,s2)!=(salt1,salt2) or state!=(c0,c1):raise StoreUnavailable('Invalid recovery WAL')
        pages[number]=data
        if count:
            if count>1024**3//size:raise StoreUnavailable('Recovery WAL capacity')
            committed={number:data for number,data in pages.items() if number<=count}
    if not count or committed is None or set(committed)!=set(range(1,count+1)):
        raise StoreUnavailable('Incomplete committed WAL page set')
    return b''.join(committed[number] for number in range(1,count+1))


def recover_state(source, destination, node_id, *, expected_inventory, apply=False, fresh=False, activity=inactive):
    if apply is not True:
        raise StoreUnavailable('Explicit recovery authorization required')
    source, destination = existing_private(source), Path(destination)
    parent = existing_private(destination.parent)
    if destination.exists() or destination.is_symlink() or destination == source:
        raise StoreUnavailable('Recovery requires a new destination')
    before = state_inventory(source)
    if inventory_digest(before) != expected_inventory or not activity(source):
        raise StoreUnavailable('Recovery state changed or active')
    with backup.volume_lease(parent), backup.operation_lock(parent):
        backup.capacity(parent, sum(r['size'] for r in before.values())*3+1048576, preflight=True)
        with tempfile.TemporaryDirectory(prefix='.incident-',dir=parent) as temporary:
            stage = Path(temporary)
            if fresh:
                if set(before) != {'node.sqlite3'} or before['node.sqlite3']['size'] != 0:
                    raise StoreUnavailable('Ambiguous empty initialization')
                target = stage/'fresh'
                target.mkdir(mode=0o700)
                from mini.store import SCHEMA
                with sqlite3.connect(target/'node.sqlite3') as db:
                    (target/'node.sqlite3').chmod(0o600)
                    for sql in SCHEMA:db.execute(sql)
                    db.executemany('INSERT INTO metadata VALUES(?,?)',[('node_id',node_id),('instance',secrets.token_hex(16)),('cursor_key',secrets.token_hex(32))])
                    db.execute('PRAGMA user_version=2');db.commit()
                    backup.validate_database(db,node_id)
            else:
                if 'node.sqlite3' not in before or 'node.sqlite3-journal' in before:
                    raise StoreUnavailable('Unsupported recovery journal')
                for name in ('node.sqlite3','node.sqlite3-wal'):
                    if name in before:
                        copy_approved(source/name, stage/name, before[name])
                if state_inventory(source) != before or not activity(source):
                    raise StoreUnavailable('Recovery state changed or active')
                if before['node.sqlite3']['size']==0:
                    wal=stage/'node.sqlite3-wal'
                    (stage/'node.sqlite3').write_bytes(reconstruct_wal(wal.read_bytes()))
                    wal.unlink()
                with sqlite3.connect(stage/'node.sqlite3') as db:
                    backup.validate_database(db,node_id)
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    db.execute('PRAGMA journal_mode=DELETE')
                target = stage/'restored'
                target.mkdir(mode=0o700)
                with sqlite3.connect(stage/'node.sqlite3') as db, sqlite3.connect(target/'node.sqlite3') as restored:
                    (target/'node.sqlite3').chmod(0o600)
                    db.backup(restored)
                    restored.execute("UPDATE metadata SET value=? WHERE key='instance'",(secrets.token_hex(16),))
                    restored.execute("UPDATE metadata SET value=? WHERE key='cursor_key'",(secrets.token_hex(32),))
                    restored.commit()
                    backup.validate_database(restored,node_id)
                    restored.execute('PRAGMA journal_mode=DELETE')
            if state_inventory(source) != before or not activity(source):
                raise StoreUnavailable('Recovery state changed or active')
            backup.fsync(target/'node.sqlite3'); backup.fsync(target)
            backup.publish(target,destination); backup.fsync(parent)
    return {'schema_version':1,'status':'complete','identity':'rotated','originals':'preserved'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('doctor','recover-state'))
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--destination');parser.add_argument('--node-id')
    parser.add_argument('--inventory-sha256');parser.add_argument('--apply',action='store_true')
    parser.add_argument('--fresh-empty',action='store_true')
    args=parser.parse_args()
    try:
        result=doctor(args.state_dir) if args.operation=='doctor' else recover_state(args.state_dir,args.destination,args.node_id,
            expected_inventory=args.inventory_sha256,apply=args.apply,fresh=args.fresh_empty)
        print(json.dumps(result,sort_keys=True));return 0
    except Exception:
        print('{"schema_version":1,"status":"blocked","error":"recovery_unavailable"}');return 1


if __name__=='__main__': raise SystemExit(main())
