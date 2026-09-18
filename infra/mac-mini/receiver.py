"""Tailscale-SSH receiver. Input and child output are never echoed or logged.

Stages a versioned runtime and secret-free launchd templates, with jobs unloaded.
Only the two remote credentials are accepted; primary local auth never travels.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tarfile
import tempfile
import shutil
import stat

if "validate_contract" not in globals():
    from bundle_contract import validate_contract


if "backend_ports_silent" not in globals():
    from socket_posture import backend_ports_silent

LABELS = ("com.wisp.mini.gateway", "com.wisp.mini.node")
if "verify_artifact" not in globals():
    from artifact_signature import verify_artifact, consume_release, publication_lock, ledger_doctor, recover_ledger, strict_json

ASSETS = {"BackendCredentials.swift", "keychain-helper.swift", "mini-launcher.swift"}
if 'manage_omlx_update' not in globals():
    from omlx_update import manage_omlx_update


def execute(argv, data=None):
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
    result = subprocess.run(argv, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env, timeout=300)
    if result.returncode:
        raise ValueError("operation_failed")


def unpack(raw):
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        files = {}
        size = 0
        for member in archive.getmembers():
            name = member.name
            if (not member.isfile() or not name.startswith("mini/") or ".." in Path(name).parts or str(Path(name)) != name
                    or name in files or len(files) >= 30000):
                raise ValueError("unsafe_bundle")
            size += member.size
            if size > 1024 * 1024 * 1024:
                raise ValueError("large_bundle")
            files[name] = archive.extractfile(member).read()
    manifest = json.loads(files["mini/bundle.json"])
    if manifest["schema_version"] != 1 or manifest["kind"] != "wisp-mini-runtime":
        raise ValueError("bundle_contract")
    validate_contract(manifest)
    rows = manifest["files"]
    hashes = {r["path"]: r["sha256"] for r in rows}
    if len(rows) != len(hashes) or set(hashes) != set(files) - {"mini/bundle.json"}:
        raise ValueError("bundle_files")
    if any(r.get("mode", 0o600) not in (0o600, 0o700) for r in rows):
        raise ValueError("bundle_mode")
    if any(hashlib.sha256(files[name]).hexdigest() != digest for name, digest in hashes.items()):
        raise ValueError("bundle_digest")
    return files


def root_path():
    root = Path.home() / ".wisp-mini"
    if root.is_symlink():
        raise ValueError("unsafe_root")
    if root.exists() and (not root.is_dir() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise ValueError("unsafe_root")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def active_jobs():
    active = set()
    for domain in ("gui/", "user/"):
        target = domain + str(os.getuid())
        result = subprocess.run(["/bin/launchctl", "print", target], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10)
        if result.returncode:
            if domain == "gui/" and result.returncode == 125:
                continue
            raise ValueError("launchd_unavailable")
        for label in LABELS:
            service_target = target + "/" + label
            service = subprocess.run(["/bin/launchctl", "print", service_target], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=10)
            if service.returncode == 0:
                active.add(service_target)
            elif service.returncode != 113:
                raise ValueError("launchd_service_unknown")
    return active


def rollback():
    root = Path.home() / '.wisp-mini'
    if not root.exists():
        if active_jobs() or root.is_symlink(): raise ValueError('ownership_unproven')
        return
    with publication_lock(root):
        _rollback_locked()


def _rollback_locked():
    active = active_jobs()
    root = Path.home() / ".wisp-mini"
    if not root.exists():
        if active or root.is_symlink():
            raise ValueError("ownership_unproven")
        return
    root = root_path()
    receipt = root / "receipt.json"
    if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_uid != os.getuid() or receipt.stat().st_mode & 0o077:
        raise ValueError("ownership_unproven")
    record = json.loads(receipt.read_text())
    if record.get("schema_version") != 1 or record.get("labels") != list(LABELS):
        raise ValueError("ownership_unproven")
    release_id = record.get("provisioning_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", release_id):
        raise ValueError("ownership_unproven")
    release = root / release_id
    marker = release / ".owner.json"
    if release.is_symlink() or marker.is_symlink() or not marker.is_file():
        raise ValueError("ownership_unproven")
    owner = json.loads(marker.read_text())
    if (owner.get("schema_version") != 2 or owner.get("provisioning_id") != release_id
            or owner.get("bundle_sha256") != record.get("bundle_sha256")
            or owner.get("source_commit") != record.get("source_commit")
            or owner.get("inventory") != inventory(release)):
        raise ValueError("ownership_unproven")
    for target in sorted(active):
        execute(["/bin/launchctl", "bootout", target])
    if active_jobs():
        raise ValueError("jobs_still_loaded")
    # Keep installed versions, user state, Keychain and firewall untouched.


def materialize(files, manifest, destination):
    modes = {row["path"]: row.get("mode", 0o600) for row in manifest["files"]}
    for name, content in files.items():
        if not name.startswith("mini/payload/"):
            continue
        relative = name.removeprefix("mini/payload/")
        path = destination / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(modes[name])
    for directory in destination.rglob("*"):
        if directory.is_dir():
            directory.chmod(0o700)


def inventory(root):
    rows = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == ".owner.json":
            continue
        info = path.lstat()
        if relative == "state":
            if not path.is_symlink() or path.resolve() != root.parent / "state":
                raise ValueError("unsafe_state_link")
            rows[relative] = {"type": "state"}
            continue
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_runtime_owner_mode")
        if stat.S_ISDIR(info.st_mode):
            rows[relative] = {"type": "directory", "mode": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            rows[relative] = {"type": "file", "mode": stat.S_IMODE(info.st_mode),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            raise ValueError("unsafe_runtime_type")
    return rows


def runtime_health(release):
    for name in ("keychain-helper", "mini-launcher"):
        execute(["/usr/bin/codesign", "--verify", "--strict", str(release / name)])
        result = subprocess.run([str(release / name), "protocol-version"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
            env={"PATH": "/usr/bin:/bin"})
        if result.returncode or result.stdout != b"wisp-mini-helper-v2\n":
            raise ValueError("unsupported_helper_version")
    execute([str(release / "venv/bin/python3"), "-I", "-B", str(release / "runtime-health.py"), str(release)])


def install(payload):
    if payload.get("schema_version") != 1 or payload.get("operation") != "stage":
        raise ValueError("invalid_protocol")
    digest = payload["bundle_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid_pin")
    raw = base64.b64decode(payload["bundle"], validate=True)
    if len(raw) > 256 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("invalid_bundle")
    files = unpack(raw)
    manifest = json.loads(files["mini/bundle.json"])
    if (manifest.get("artifact_type") != "offline-runtime"
            or manifest["source_commit"] != payload.get("source_commit")
            or manifest["provenance"].get("strict_toolchain") is not True):
        raise ValueError("qualified_offline_candidate_required")
    publisher = payload.get("publisher", {})
    if not isinstance(publisher, dict) or set(publisher) != {"envelope", "trust", "trust_sha256", "key_id", "release_sequence"}:
        raise ValueError("publisher_authentication_required")
    import time
    authenticated = verify_artifact(raw, base64.b64decode(publisher["envelope"], validate=True),
        base64.b64decode(publisher["trust"], validate=True), trust_sha256=publisher["trust_sha256"],
        key_id=publisher["key_id"], source_commit=payload["source_commit"], bundle_sha256=digest,
        release_sequence=publisher["release_sequence"], now=int(time.time()))
    secrets = payload["credentials"]
    if (set(secrets) != {"mini-inference", "mini-node"} or len(set(secrets.values())) != 2
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in secrets.values())):
        raise ValueError("invalid_credentials")
    node_id = payload["node_id"]
    if not re.fullmatch(r"n[A-Za-z0-9]+", node_id) or os.getuid() == 0:
        raise ValueError("invalid_user_or_node")
    if active_jobs():
        raise ValueError("jobs_must_be_disabled")
    if not backend_ports_silent():
        raise ValueError("backend_ports_must_be_silent")
    root = root_path()
    import fcntl
    fd = os.open(root / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError("unsafe_install_lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # The sequence ledger and every publication share one operation lock.
        # A losing concurrent installer must retry authentication/consumption.
        consume_release(authenticated, root / "artifact-releases.json", now=int(time.time()))
        provisioning_id = hashlib.sha256(json.dumps({"bundle": digest, "node_id": node_id,
            "release_sequence": authenticated.release_sequence,
            "statement_sha256": authenticated.statement_sha256}, sort_keys=True).encode()).hexdigest()
        release = root / provisioning_id
        if release.is_symlink() or release.exists() and not release.is_dir():
            raise ValueError("unsafe_release")
        owner = {"schema_version": 2, "provisioning_id": provisioning_id, "node_id": node_id,
                 "source_commit": manifest["source_commit"], "bundle_sha256": digest,
                 "release_sequence": authenticated.release_sequence,
                 "statement_sha256": authenticated.statement_sha256,
                 "provenance": manifest["provenance"]}
        # Build an independent expected inventory from authenticated artifact bytes
        # even for repeat staging; a forged owner marker cannot bless changed code.
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as temporary:
            stage = Path(temporary)
            materialize(files, manifest, stage)
            (stage / "launchd").mkdir(mode=0o700)
            for kind, label in zip(("gateway", "node"), LABELS):
                plist = {"Label": label, "ProgramArguments": [str(release / "mini-launcher"), kind, str(release), node_id],
                         "RunAtLoad": False, "KeepAlive": False, "Disabled": True,
                         "ProcessType": "Background", "Umask": 0o077}
                dest = stage / "launchd" / (label + ".plist")
                dest.write_bytes(plistlib.dumps(plist))
                dest.chmod(0o600)
            state = root / "state"
            if state.is_symlink() or state.exists() and (not state.is_dir() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077):
                raise ValueError("unsafe_state")
            # State is deliberately outside the immutable executable inventory.
            (stage / "state").symlink_to(state, target_is_directory=True)
            expected = inventory(stage)
            owner["inventory"] = expected
            if release.exists():
                marker = release / ".owner.json"
                if marker.is_symlink() or not marker.is_file() or marker.stat().st_mode & 0o077 or json.loads(marker.read_text()) != owner or inventory(release) != expected:
                    raise ValueError("existing_release_unproven")
                runtime_health(release)
            else:
                runtime_health(stage)
                marker = stage / ".owner.json"
                marker.write_text(json.dumps(owner, sort_keys=True))
                marker.chmod(0o600)
                # The only publication is an atomic directory rename, after every
                # file, signature, import and synthetic health check passed.
                os.rename(stage, release)
                stage.mkdir(mode=0o700)  # TemporaryDirectory cleanup owns only this.
                try:
                    runtime_health(release)
                except BaseException:
                    shutil.rmtree(release)
                    raise
            state.mkdir(mode=0o700, exist_ok=True)
            execute([str(release / "keychain-helper"), "import-mini"], json.dumps(secrets).encode())
            if not backend_ports_silent():
                raise ValueError("backend_ports_must_be_silent")
            if active_jobs():
                raise ValueError("jobs_must_be_disabled")
            receipt = {"schema_version": 1, "bundle_sha256": digest, "provisioning_id": provisioning_id,
                       "source_commit": manifest["source_commit"], "provenance": manifest["provenance"],
                       "release_sequence": authenticated.release_sequence,
                       "statement_sha256": authenticated.statement_sha256,
                       "labels": list(LABELS), "jobs_enabled": False, "gateway_qualification_required": True}
            fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=root)
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump(receipt, output, sort_keys=True)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, root / "receipt.json")
                directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def private_document(path):
    fd = os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 8*1024*1024): raise ValueError('unsafe_management_state')
        return strict_json(source.read(8*1024*1024+1))


def unused_release(path, *, pinned=None):
    result=subprocess.run(['/usr/sbin/lsof','-nP','-Fpfa','+D',str(path)],stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,timeout=15,env={'PATH':'/usr/bin:/bin','LC_ALL':'C'})
    if result.returncode==1 and not result.stdout.strip() and not result.stderr.strip():return True
    if result.returncode not in (0,1) or result.stderr.strip() or pinned is None:return False
    own=os.fstat(pinned);target=Path(path).lstat()
    if (not stat.S_ISDIR(own.st_mode) or (own.st_dev,own.st_ino)!=(target.st_dev,target.st_ino)
            or own.st_uid!=os.getuid()):return False
    # Exempt only this exact pinned read-only directory descriptor. Another
    # descriptor held by this same process is still activity and blocks cleanup.
    lines=result.stdout.decode('ascii').splitlines()
    return lines==['p'+str(os.getpid()),'f'+str(pinned),'ar']


def release_inventory(root, *, retain=2, minimum_age=86400, now=None):
    import time
    now=time.time() if now is None else now
    if type(retain) is not int or retain<2 or type(minimum_age) is not int or minimum_age<86400:
        raise ValueError('unsafe_retention')
    root=Path(root)
    receipt=private_document(root/'receipt.json')
    ledger=ledger_doctor(root)
    if (ledger['status']!='initialized' or receipt.get('schema_version')!=1
            or receipt.get('labels')!=list(LABELS) or type(receipt.get('release_sequence')) is not int
            or receipt['release_sequence']>ledger['release_sequence']): raise ValueError('management_ownership_unproven')
    current=receipt.get('provisioning_id')
    if not isinstance(current,str) or not re.fullmatch('[0-9a-f]{64}',current): raise ValueError('management_ownership_unproven')
    rows, ambiguous, sequences=[],[],set()
    for path in sorted(root.iterdir()):
        if path.name.startswith(('.stage-','.reclaim-')):
            ambiguous.append(path.name);continue
        if not re.fullmatch('[0-9a-f]{64}',path.name): continue
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise ValueError('unsafe_release')
        owner=private_document(path/'.owner.json')
        expected=hashlib.sha256(json.dumps({'bundle':owner.get('bundle_sha256'),'node_id':owner.get('node_id'),
            'release_sequence':owner.get('release_sequence'),'statement_sha256':owner.get('statement_sha256')},sort_keys=True).encode()).hexdigest()
        if (owner.get('schema_version')!=2 or owner.get('provisioning_id')!=path.name or expected!=path.name
                or owner.get('inventory')!=inventory(path) or type(owner.get('release_sequence')) is not int
                or owner['release_sequence'] in sequences or not 0<owner['release_sequence']<=ledger['release_sequence']):
            raise ValueError('release_ownership_unproven')
        sequences.add(owner['release_sequence'])
        if owner['release_sequence']==ledger['release_sequence']:
            doc=private_document(root/'artifact-releases.json')
            if doc['statement_sha256']!=owner.get('statement_sha256'): raise ValueError('release_ledger_mismatch')
        if path.name==current and any(owner.get(k)!=receipt.get(k) for k in ('source_commit','bundle_sha256','release_sequence','statement_sha256')):
            raise ValueError('receipt_mismatch')
        rows.append({'id':path.name,'sequence':owner['release_sequence'],'device':info.st_dev,'inode':info.st_ino,
                     'mtime_ns':info.st_mtime_ns,'owner_sha256':hashlib.sha256(json.dumps(owner,sort_keys=True).encode()).hexdigest(),
                     'old_enough':now-info.st_mtime>=minimum_age})
    if not any(row['id']==current for row in rows): raise ValueError('current_release_missing')
    retained={row['id'] for row in sorted(rows,key=lambda r:r['sequence'],reverse=True)[:retain]}|{current}
    for row in rows:row['eligible']=row['old_enough'] and row['id'] not in retained
    result={'schema_version':1,'current':current,'ledger':ledger,'releases':rows,'ambiguous':ambiguous,
            'retain':retain,'minimum_age':minimum_age}
    result['inventory_sha256']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return result


def entry_identity(info):
    value={'device':info.st_dev,'inode':info.st_ino,'mode':info.st_mode,'uid':info.st_uid}
    if not stat.S_ISDIR(info.st_mode):
        value.update(size=info.st_size,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns,nlink=info.st_nlink)
    return value


def frozen_tree(directory, names=None):
    rows={}
    for name in sorted(os.listdir(directory) if names is None else names):
        info=os.stat(name,dir_fd=directory,follow_symlinks=False)
        if info.st_uid!=os.getuid():raise ValueError('reclamation_owner_changed')
        row=entry_identity(info)
        if stat.S_ISDIR(info.st_mode):
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            try:
                if entry_identity(os.fstat(child))!=row:raise ValueError('reclamation_changed')
                row['children']=frozen_tree(child)
            finally:os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            with os.fdopen(fd,'rb') as source:
                if entry_identity(os.fstat(source.fileno()))!=row:raise ValueError('reclamation_changed')
                h=hashlib.sha256()
                for block in iter(lambda:source.read(1048576),b''):h.update(block)
                if entry_identity(os.fstat(source.fileno()))!=row:raise ValueError('reclamation_changed')
                row['sha256']=h.hexdigest()
        elif stat.S_ISLNK(info.st_mode) and name=='state':
            row['target']=os.readlink(name,dir_fd=directory)
        else:raise ValueError('reclamation_type_changed')
        if entry_identity(os.stat(name,dir_fd=directory,follow_symlinks=False))!=entry_identity(info):
            raise ValueError('reclamation_changed')
        rows[name]=row
    return rows


def approved_remaining(actual, approved):
    # Missing entries are permitted only after durable reclamation intent. No
    # replacement, new entry, changed bytes, or changed link target is permitted.
    if set(actual)-set(approved):return False
    for name,row in actual.items():
        old=approved[name]
        if {k:v for k,v in row.items() if k!='children'}!={k:v for k,v in old.items() if k!='children'}:return False
        if 'children' in row and not approved_remaining(row['children'],old['children']):return False
    return True


def remove_owned_tree(directory, approved):
    if not approved_remaining(frozen_tree(directory),approved):raise ValueError('reclamation_changed')
    for name in sorted(os.listdir(directory)):
        row=approved[name]
        info=os.stat(name,dir_fd=directory,follow_symlinks=False)
        if entry_identity(info)!={k:v for k,v in row.items() if k not in ('children','sha256','target')}:
            raise ValueError('reclamation_changed')
        if stat.S_ISDIR(info.st_mode):
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            try:
                if entry_identity(os.fstat(child))!=entry_identity(info):raise ValueError('reclamation_changed')
                remove_owned_tree(child,row['children'])
            finally:os.close(child)
            if entry_identity(os.stat(name,dir_fd=directory,follow_symlinks=False))!=entry_identity(info):raise ValueError('reclamation_changed')
            os.rmdir(name,dir_fd=directory)
        else:
            # Re-read content/identity immediately before unlink. No path-based
            # traversal or chmod can escape the pinned private release tree.
            if frozen_tree(directory,[name]).get(name)!=row:raise ValueError('reclamation_changed')
            os.unlink(name,dir_fd=directory)
        os.fsync(directory)


def management_receipt(root, name, value):
    fd,temporary=tempfile.mkstemp(prefix='.management-',dir=root)
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(value,output,sort_keys=True);output.flush();os.fsync(output.fileno())
        os.replace(temporary,root/name)
        directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def exclusive_rename(directory,source,destination):
    import ctypes
    rename=ctypes.CDLL(None,use_errno=True).renameatx_np
    rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    rename.restype=ctypes.c_int
    if rename(directory,source.encode(),directory,destination.encode(),4)!=0:
        raise ValueError('exclusive_rename_unavailable')
    os.fsync(directory)


def reclaim_releases(root, ids, *, expected_inventory, apply=False, retain=2, minimum_age=86400,
                     activity=unused_release, jobs=active_jobs, now=None):
    if (apply is not True or not isinstance(ids,list) or not ids or len(set(ids))!=len(ids)
            or any(not isinstance(i,str) or not re.fullmatch('[0-9a-f]{64}',i) for i in ids)):
        raise ValueError('explicit_reclamation_required')
    root=Path(root)
    def inactive_at(path,pinned=None):
        return unused_release(path,pinned=pinned) if activity is unused_release else activity(path)
    request={'ids':ids,'inventory_sha256':expected_inventory,'retain':retain,'minimum_age':minimum_age}
    key=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
    name='.reclamation-'+key+'.json'
    with publication_lock(root) as directory:
        if jobs():raise ValueError('reclamation_unproven')
        fs=os.statvfs(root)
        if fs.f_bavail*fs.f_frsize<65536:raise ValueError('reclamation_capacity_unavailable')
        try:journal=private_document(root/name)
        except FileNotFoundError:
            before=release_inventory(root,retain=retain,minimum_age=minimum_age,now=now)
            eligible={row['id']:row for row in before['releases'] if row['eligible']}
            if before['inventory_sha256']!=expected_inventory or any(i not in eligible for i in ids):raise ValueError('reclamation_unproven')
            trees={}
            for rid in ids:
                if not inactive_at(root/rid):raise ValueError('release_active')
                child=os.open(rid,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                try:
                    info=os.fstat(child)
                    if (info.st_dev,info.st_ino)!=(eligible[rid]['device'],eligible[rid]['inode']):raise ValueError('reclamation_changed')
                    trees[rid]={'identity':entry_identity(info),'entries':frozen_tree(child)}
                finally:os.close(child)
            if release_inventory(root,retain=retain,minimum_age=minimum_age,now=now)!=before or jobs():raise ValueError('reclamation_changed')
            journal={'schema_version':1,'request':request,'receipt':private_document(root/'receipt.json'),
                     'ledger':ledger_doctor(root),'trees':trees,'removed':[]}
            management_receipt(root,name,journal)
        if (journal.get('schema_version')!=1 or journal.get('request')!=request
                or journal.get('receipt')!=private_document(root/'receipt.json')
                or journal.get('ledger')!=ledger_doctor(root)):
            raise ValueError('reclamation_journal_changed')
        for rid in ids:
            slot='.reclaim-'+rid
            original=os.path.lexists(root/rid);renamed=os.path.lexists(root/slot)
            if rid in journal['removed']:
                if original or renamed:raise ValueError('reclamation_changed')
                continue
            if not original and not renamed:
                # An interrupted fsynced final removal is safe to acknowledge.
                journal['removed'].append(rid);management_receipt(root,name,journal);continue
            if original and renamed:raise ValueError('reclamation_changed')
            location=rid if original else slot
            child=os.open(location,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            try:
                tree=journal['trees'][rid]
                if entry_identity(os.fstat(child))!=tree['identity']:raise ValueError('reclamation_changed')
                actual=frozen_tree(child)
                if (actual!=tree['entries'] if original else not approved_remaining(actual,tree['entries'])):raise ValueError('reclamation_changed')
                if not inactive_at(root/location,child) or jobs():raise ValueError('release_active')
                if journal['receipt']!=private_document(root/'receipt.json') or journal['ledger']!=ledger_doctor(root):raise ValueError('reclamation_changed')
                if original:exclusive_rename(directory,rid,slot)
                if not inactive_at(root/slot,child) or jobs():raise ValueError('release_active_after_rename')
                if entry_identity(os.stat(slot,dir_fd=directory,follow_symlinks=False))!=tree['identity']:raise ValueError('reclamation_changed')
                remove_owned_tree(child,tree['entries'])
                if entry_identity(os.stat(slot,dir_fd=directory,follow_symlinks=False))!=tree['identity']:raise ValueError('reclamation_changed')
                os.rmdir(slot,dir_fd=directory);os.fsync(directory)
            finally:os.close(child)
            journal['removed'].append(rid);management_receipt(root,name,journal)
    return {'schema_version':1,'status':'complete','removed':journal['removed'],'jobs_enabled':False}


def management_identity(payload):
    result=subprocess.run(['/opt/homebrew/bin/tailscale','status','--json'],stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,timeout=10,env={'PATH':'/usr/bin:/bin','HOME':str(Path.home())})
    if result.returncode or len(result.stdout)>1024*1024:raise ValueError('management_identity_unavailable')
    status=strict_json(result.stdout)
    own=status.get('Self',{})
    if (status.get('BackendState')!='Running' or own.get('ID')!=payload.get('node_id')
            or payload.get('ip') not in own.get('TailscaleIPs',[]) or 'tag:wisp-inference' not in own.get('Tags',[])):
        raise ValueError('management_identity_mismatch')


def management_read(argv):
    result=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,
        env={'HOME':str(Path.home()),'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'})
    if result.returncode or len(result.stdout)>8*1024*1024:raise ValueError('management_command_unavailable')
    return result.stdout


def management_jobs(root, report):
    active=active_jobs()
    for target in active:
        if not target.startswith('gui/'+str(os.getuid())+'/'):raise ValueError('management_job_scope')
        label=target.rsplit('/',1)[1]
        kind='gateway' if label==LABELS[0] else 'node'
        release=root/report['current']
        owner=private_document(release/'.owner.json')
        raw=management_read(['/bin/launchctl','print',target]).decode('utf-8')
        arguments=re.findall(r'(?m)^\s*arguments = \{\n([^}]+)\}',raw)
        if (len(arguments)!=1 or [x.strip() for x in arguments[0].splitlines() if x.strip()]!=
                [str(release/'mini-launcher'),kind,str(release),owner['node_id']]):raise ValueError('management_job_unproven')
    return active


def manage(payload, *, verify_identity=management_identity):
    """Exact-machine SSH management independent of either HTTP service."""
    if (not isinstance(payload,dict) or set(payload)-{'schema_version','operation','node_id','ip','apply','arguments'}
            or payload.get('schema_version')!=1 or not re.fullmatch('n[A-Za-z0-9]+',payload.get('node_id',''))):
        raise ValueError('invalid_management_frame')
    verify_identity(payload)
    root=Path.home()/'.wisp-mini'
    # Diagnostics do not create state or require either service to answer.
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:
        raise ValueError('management_root_unproven')
    operation=payload['operation'];arguments=payload.get('arguments',{})
    if not isinstance(arguments,dict):raise ValueError('invalid_management_arguments')
    if operation.startswith('omlx-'):
        if operation not in ('omlx-status','omlx-select') and payload.get('apply') is not True:
            raise ValueError('explicit_management_authorization_required')
        return manage_omlx_update(root,operation,arguments,private_document)
    if operation=='diagnose':
        result={'schema_version':1,'status':'complete','ledger':ledger_doctor(root),
                'omlx_adapter':'independent_qualification_required','management':'ssh_independent_of_services'}
        try:result['releases']=release_inventory(root)
        except Exception:result['releases']={'status':'blocked','error':'release_ownership_unproven'}
        return result
    if payload.get('apply') is not True and operation not in ('releases','database-doctor','credential-status'):
        raise ValueError('explicit_management_authorization_required')
    if operation=='recover-ledger':
        if set(arguments)!={'authorization','authorization_sha256'}:raise ValueError('invalid_management_arguments')
        return recover_ledger(root,arguments['authorization'],arguments['authorization_sha256'],apply=True)
    if operation=='releases':return release_inventory(root)
    if operation=='reclaim':
        if set(arguments)!={'ids','inventory_sha256'}:raise ValueError('invalid_management_arguments')
        return reclaim_releases(root,arguments['ids'],expected_inventory=arguments['inventory_sha256'],apply=True)
    with publication_lock(root):
        # Every inspection and effect shares installation/restart/recovery's lock.
        # A durable intent prevents an interrupted command being silently replayed.
        if operation in ('restart','rollback','recover-state'):
            nonce=arguments.get('operation_id')
            if not isinstance(nonce,str) or not re.fullmatch('[0-9a-f]{64}',nonce):
                raise ValueError('management_operation_id_required')
            name='.operation-'+nonce+'.json'
            request_hash=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
            try:prior=private_document(root/name)
            except FileNotFoundError:prior=None
            if prior is not None:
                if prior.get('request_sha256')!=request_hash:raise ValueError('management_operation_conflict')
                if prior.get('status')=='complete':return prior['result']
                raise ValueError('management_recovery_required')
            record={'schema_version':1,'request_sha256':request_hash,'status':'started','operation':operation}
            management_receipt(root,name,record)
            arguments={k:v for k,v in arguments.items() if k!='operation_id'}
            result=managed_release_operation(root,payload,operation,arguments)
            record.update(status='complete',result=result);management_receipt(root,name,record)
            return result
        return managed_release_operation(root,payload,operation,arguments)


def managed_release_operation(root,payload,operation,arguments):
    report=release_inventory(root)
    release=root/report['current']
    owner=private_document(release/'.owner.json')
    if owner['node_id']!=payload['node_id']:raise ValueError('management_node_mismatch')
    if operation=='rollback':
        management_jobs(root,report);_rollback_locked()
        return {'schema_version':1,'status':'complete','jobs_enabled':False}
    if operation=='restart':
        if set(arguments)!={'service'} or arguments['service'] not in ('gateway','node'):raise ValueError('invalid_management_arguments')
        active=management_jobs(root,report)
        label=LABELS[0 if arguments['service']=='gateway' else 1]
        target='gui/'+str(os.getuid())+'/'+label
        if target not in active:raise ValueError('restart_requires_existing_qualified_job')
        management_read(['/bin/launchctl','kickstart','-k',target])
        if management_jobs(root,report)!=active:raise ValueError('restart_unverified')
        return {'schema_version':1,'status':'complete','restart':'requested_existing_job','readiness':'separate_qualification_required'}
    if operation=='credential-status':
        if arguments:raise ValueError('invalid_management_arguments')
        status=strict_json(management_read([str(release/'keychain-helper'),'status']))
        # The helper emits fixed booleans only. Never forward arbitrary child output.
        if status not in ({'credentials':'ready'},{'credentials':'missing'}):raise ValueError('credential_status_unavailable')
        return {'schema_version':1,'status':'complete','credentials_available':status['credentials']=='ready',
                'adoption':'independently_qualified_identity_required'}
    if operation in ('database-doctor','recover-state'):
        if operation=='database-doctor':
            if arguments:raise ValueError('invalid_management_arguments')
            argv=[str(release/'venv/bin/python3'),'-I','-B','-c',
                'import sys;sys.path.insert(0,sys.argv.pop(1));from mini.recovery import main;raise SystemExit(main())',
                str(release/'runtime'),'doctor','--state-dir',str(root/'state')]
        else:
            if set(arguments)!={'destination','inventory_sha256','fresh_empty'} or not re.fullmatch('recovered-[a-z0-9-]{1,48}',arguments['destination']) or type(arguments['fresh_empty']) is not bool:
                raise ValueError('invalid_management_arguments')
            if active_jobs():raise ValueError('database_recovery_requires_stopped_jobs')
            argv=[str(release/'venv/bin/python3'),'-I','-B','-c',
                'import sys;sys.path.insert(0,sys.argv.pop(1));from mini.recovery import main;raise SystemExit(main())',str(release/'runtime'),
                'recover-state','--state-dir',str(root/'state'),'--destination',str(root/arguments['destination']),
                '--node-id',payload['node_id'],'--inventory-sha256',arguments['inventory_sha256'],'--apply']
            if arguments['fresh_empty']:argv.append('--fresh-empty')
        result=strict_json(management_read(argv))
        if not isinstance(result,dict) or result.get('schema_version')!=1:raise ValueError('management_result_invalid')
        return result
    raise ValueError('unsupported_management_operation')


def main():
    try:
        os.umask(0o077)
        if sys.argv[1:] == ['manage']:
            raw=sys.stdin.buffer.read(1024*1024+1)
            if len(raw)>1024*1024:raise ValueError('large_management_frame')
            print(json.dumps(manage(strict_json(raw)),sort_keys=True));return 0
        elif sys.argv[1:] == ["rollback"]:
            rollback()
        elif not sys.argv[1:]:
            raw = sys.stdin.buffer.read(400 * 1024 * 1024 + 1)
            if len(raw) > 400 * 1024 * 1024:
                raise ValueError("large_frame")
            install(json.loads(raw))
        else:
            raise ValueError("invalid_operation")
        print('{"schema_version":1,"status":"complete","jobs_enabled":false,"gateway_qualification_required":true}')
    except Exception:
        print('{"schema_version":1,"status":"blocked"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
