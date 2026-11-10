"""Bounded disk copies of durable model objects; no remote deletion.

Only unchanged files with a verified remote identity can be evicted. All
model readers and the uploader/flush share the same cross-process lock.
Untracked, modified, active and in-use files are never eviction candidates.
"""
from contextlib import contextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

from backend.app.services.restore_cache import DIRECTORIES, MAX_BYTES, MAX_FILES, MODEL_BINARIES

INTERNAL='.traffic-cache'
_threads=threading.local()
_mutex=threading.Lock()
_locks={}


def internal(path):return INTERNAL in Path(path).parts


def fingerprint(path):
    s=path.stat(follow_symlinks=False)
    return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]


def sha256_file(path):
    sha=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(256*1024):sha.update(block)
    return sha.hexdigest()


def configured(model_root):
    value=os.environ.get('TRAFFIC_ARTIFACT_CACHE_BYTES')
    return ArtifactCache(model_root,budget=int(value)) if value is not None else None


class ArtifactCache:
    def __init__(self,model_root,*,budget=MAX_BYTES,reserve=256*1024**2):
        if type(budget) is not int or not 0<budget<=MAX_BYTES:
            raise ValueError('Invalid model disk cache budget')
        self.root=Path(model_root).absolute()
        if any(p.is_symlink() for p in (self.root,*self.root.parents)):
            raise RuntimeError('Model cache cannot follow symlinks')
        self.budget,self.reserve=budget,reserve
        self.meta=self.root/INTERNAL
        self.index=self.meta/'index.json'

    @contextmanager
    def hold(self,timeout=30):
        self.root.mkdir(parents=True,exist_ok=True)
        if self.meta.is_symlink():raise RuntimeError('Unsafe model cache metadata')
        self.meta.mkdir(mode=0o700,exist_ok=True)
        key=str(self.root)
        with _mutex:lock=_locks.setdefault(key,threading.RLock())
        if not lock.acquire(timeout=timeout):raise TimeoutError('Model cache lock deadline exceeded')
        depths=getattr(_threads,'depths',None)
        if depths is None:depths=_threads.depths={}
        fd=None
        try:
            if not depths.get(key):
                path=self.meta/'lock'
                if path.is_symlink():raise RuntimeError('Unsafe model cache lock')
                fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
                if not os.fstat(fd).st_size:os.write(fd,b'0')
                deadline=time.monotonic()+timeout
                while True:
                    try:
                        if os.name=='nt':
                            import msvcrt
                            os.lseek(fd,0,0);msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
                        else:
                            import fcntl
                            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        break
                    except (OSError,BlockingIOError):
                        if time.monotonic()>=deadline:raise TimeoutError('Model cache lock deadline exceeded') from None
                        time.sleep(.05)
            depths[key]=depths.get(key,0)+1
            try:yield self
            finally:depths[key]-=1
        finally:
            if fd is not None:
                # Closing the descriptor also releases an OS lock on failures.
                os.close(fd)
            lock.release()

    def path(self,relative):
        parts=str(relative).split('/')
        if (not parts or any(p in {'','.','..',INTERNAL} for p in parts)
                or '\\' in str(relative) or Path(relative).is_absolute()):
            raise ValueError('Unsafe model cache path')
        path=self.root.joinpath(*parts)
        if not path.is_relative_to(self.root) or any(':' in p or '\x00' in p for p in parts):
            raise ValueError('Unsafe model cache path')
        if any(p.is_symlink() for p in (path,*path.parents)):
            raise RuntimeError('Model cache cannot follow symlinks')
        return path

    def records(self):
        if self.index.is_symlink():raise RuntimeError('Unsafe model cache index')
        if not self.index.exists():return {}
        if self.index.stat().st_size>8*1024**2:raise RuntimeError('Model cache index exceeds its budget')
        value=json.loads(self.index.read_text(encoding='utf-8'))
        if value.get('schema')!='traffic-model-disk-cache/v1' or not isinstance(value.get('entries'),dict):
            raise RuntimeError('Invalid model cache index')
        if len(value['entries'])>MAX_FILES:raise RuntimeError('Model cache index exceeds its budget')
        return value['entries']

    def write(self,records):
        temporary=self.meta/(uuid.uuid4().hex+'.tmp')
        try:
            payload=json.dumps({'schema':'traffic-model-disk-cache/v1','entries':records})
            if len(payload.encode('utf-8'))>8*1024**2:
                raise RuntimeError('Model cache index exceeds its budget')
            temporary.write_text(payload,encoding='utf-8')
            os.replace(temporary,self.index)
        finally:temporary.unlink(missing_ok=True)

    def record(self,path,*,etag,sha256):
        relative=path.relative_to(self.root).as_posix()
        if not etag or path.suffix.lower() not in MODEL_BINARIES:return
        records=self.records()
        # Prune vanished entries, bounded by the same file limit as restore.
        records={k:v for k,v in records.items() if self.path(k).is_file()}
        if relative not in records and len(records)>=MAX_FILES:
            raise RuntimeError('Model cache index exceeds its budget')
        records[relative]={'fingerprint':fingerprint(path),'etag':etag,'sha256':sha256,'used':time.time_ns()}
        self.write(records)

    def clean(self,path):
        relative=path.relative_to(self.root).as_posix()
        record=self.records().get(relative)
        return record if record and record['fingerprint']==fingerprint(path) else None

    def usage(self):
        total=count=0;started=time.monotonic()
        for name in DIRECTORIES:
            for path in (self.root.parent/name).rglob('*'):
                if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                    raise RuntimeError('Unsafe artifact cache tree')
                if not path.is_file():continue
                total+=path.stat().st_size;count+=1
                if count>100000 or time.monotonic()-started>30:
                    raise RuntimeError('Model disk inventory observation budget exceeded')
        return total

    def make_room(self,size,*,pinned,client,bucket):
        if type(size) is not int or not 0<=size<=self.budget:
            raise RuntimeError('Model exceeds the disk cache budget')
        records=self.records();total=self.usage();deadline=time.monotonic()+30;probes=0
        # Bound index growth during the atomic publication of this new entry.
        def enough():return total+size+65536<=self.budget and shutil.disk_usage(self.root).free>=size+self.reserve
        for key,entry in sorted(records.items(),key=lambda pair:pair[1]['used']):
            if enough():break
            if key in pinned:continue
            path=self.path(key)
            if not path.is_file() or path.suffix.lower() not in MODEL_BINARIES:continue
            if fingerprint(path)!=entry['fingerprint']:continue
            if sha256_file(path)!=entry['sha256']:continue
            probes+=1
            if probes>128 or time.monotonic()>deadline:
                raise RuntimeError('Model cache eviction observation budget exceeded')
            # A deleted/overwritten remote object is not a durable backup of
            # these bytes. Provider errors must never authorize eviction.
            head=client.head_object(Bucket=bucket,Key='models/'+key)
            if head.get('ETag')!=entry['etag'] or head['ContentLength']!=path.stat().st_size:continue
            length=path.stat().st_size
            path.unlink();total-=length;records.pop(key)
        self.write(records)
        if not enough():raise RuntimeError('Insufficient disk budget for the requested model')

    def ensure(self,relative,*,client,bucket,pinned=()):
        with self.hold():
            path=self.path(relative)
            if path.is_file() and path.stat().st_size>0:
                records=self.records()
                used=time.time_ns()
                if relative in records and used-records[relative]['used']>30*10**9:
                    records[relative]['used']=used
                    try:self.write(records)
                    except OSError:pass  # Recency is optional; cached inference can still read.
                return path
            head=client.head_object(Bucket=bucket,Key='models/'+relative)
            if not head.get('ETag'):raise RuntimeError('Model object has no stable identity')
            if type(head['ContentLength']) is not int or head['ContentLength']<=0:
                raise RuntimeError('Invalid model object size')
            self.make_room(head['ContentLength'],pinned=set(pinned)|{relative},client=client,bucket=bucket)
            path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
            count=0;sha=hashlib.sha256()
            try:
                response=client.get_object(Bucket=bucket,Key='models/'+relative,IfMatch=head['ETag'])
                with closing(response['Body']) as body,temporary.open('xb') as output:
                    while block:=body.read(256*1024):
                        count+=len(block)
                        if count>head['ContentLength']:raise RuntimeError('Model object exceeds its declared size')
                        sha.update(block);output.write(block)
                expected=head.get('Metadata',{}).get('sha256')
                if count!=head['ContentLength'] or expected and expected!=sha.hexdigest():
                    raise RuntimeError('Model object differs from its declared identity')
                os.replace(temporary,path)
                self.record(path,etag=head['ETag'],sha256=sha.hexdigest())
                return path
            finally:temporary.unlink(missing_ok=True)
