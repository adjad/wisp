"""Shared strict Darwin oMLX process and established-connection attribution."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
import tempfile
import time
import ctypes
import ipaddress


class AuthRefused(Exception):
    pass


def runtime_manifest(root):
    """Pin the complete materialized runtime, including interpreter and packages.

    External links and writable code are not a qualified runtime. Snapshot every
    inode before/after reading; a tree modified during inspection is refused.
    """
    root = Path(root)
    if root.resolve(strict=True) != root:
        raise AuthRefused('runtime_tree_unqualified')
    rows, inventory, total = [], {}, 0
    def snapshot():
        found = {}
        for path in [root, *sorted(root.rglob('*'))]:
            info = path.lstat()
            if (len(found) >= 50000 or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                    or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise AuthRefused('runtime_tree_unqualified')
            found[path] = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
                           info.st_mtime_ns, info.st_ctime_ns)
        return found
    inventory = snapshot()
    # The approved adapter must materialize a standalone isolated interpreter.
    # A console shim pointing to Homebrew, a venv's external base interpreter,
    # or executable .pth startup imports cannot be covered by this tree pin.
    shim = root / 'bin/omlx'
    interpreter = root / 'bin/python3'
    expected_shim = ('#!' + str(interpreter) + ' -I\nfrom omlx.cli import main\nmain()\n').encode()
    with interpreter.open('rb') as source:
        interpreter_magic = source.read(4)
    if (shim not in inventory or interpreter not in inventory or shim.read_bytes() != expected_shim
            or interpreter_magic not in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe')
            or any(path.name in ('pyvenv.cfg', 'sitecustomize.py', 'usercustomize.py')
                   or path.suffix == '.pth' for path in inventory)):
        raise AuthRefused('runtime_import_closure_unqualified')
    for path, info in inventory.items():
        digest = None
        if stat.S_ISREG(info[2]):
            total += info[4]
            if total > 8 * 1024**3:
                raise AuthRefused('runtime_tree_unqualified')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                current = os.fstat(stream.fileno())
                if (current.st_dev, current.st_ino) != info[:2]:
                    raise AuthRefused('runtime_tree_changed')
                h = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
                digest = h.hexdigest()
        rows.append([path.relative_to(root).as_posix(), stat.S_IMODE(info[2]),
                     'file' if digest is not None else 'directory', digest])
    if snapshot() != inventory:
        raise AuthRefused('runtime_tree_changed')
    return hashlib.sha256(json.dumps(rows, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def read_private(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit):
            raise AuthRefused('unsafe_local_state')
        raw = os.read(fd, limit + 1)
        if len(raw) != info.st_size:
            raise AuthRefused('changed_local_state')
        return raw
    finally:
        os.close(fd)


def atomic_private(prep, path, raw):
    fd, temporary = tempfile.mkstemp(prefix='.auth-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        prep.sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def process_identity(pid, uid):
    """Darwin PROC_PIDTBSDINFO: exact effective/real/saved UID and incarnation."""
    class BSDInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in
            ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
        _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
        _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'jobc', 'tdev', 'tpgid', 'nice')]
        _fields_ += [('start_seconds', ctypes.c_uint64), ('start_microseconds', ctypes.c_uint64)]
    try:
        if ctypes.sizeof(BSDInfo) != 136:
            raise AuthRefused('process_identity_unsupported')
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        query = library.proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        info = BSDInfo()
        if (query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info)
                or info.pid != pid or (info.uid, info.ruid, info.svuid) != (uid, uid, uid)
                or not info.start_seconds or info.start_microseconds >= 1000000):
            raise AuthRefused('process_identity_unqualified')
        return (pid, uid, info.start_seconds, info.start_microseconds)
    except (OSError, AttributeError):
        raise AuthRefused('process_identity_unsupported') from None


def socket_identity(sock, port=8000):
    try:
        fd, local, remote = sock.fileno(), sock.getsockname(), sock.getpeername()
        if (type(fd) is not int or fd < 0 or remote != ('127.0.0.1', port)
                or len(local) != 2 or local[0] != '127.0.0.1'
                or type(local[1]) is not int or not 0 < local[1] < 65536 or local[1] == port):
            raise AuthRefused('connected_socket_unqualified')
        return fd, local, remote
    except (OSError, AttributeError, TypeError):
        raise AuthRefused('connected_socket_unqualified') from None


def connection_owners(raw):
    """Strict lsof process/file records; inspect all owners, never filter by PID."""
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > 1024 * 1024:
            raise ValueError
        records, process, item = [], {}, {}
        def finish():
            if not item:
                return
            if set(process) != {'p', 'u'} or set(item) != {'f', 't', 'P', 'n', 'T'}:
                raise ValueError
            if item['t'] != 'IPv4' or item['P'] != 'TCP' or item['T'] != 'ST=ESTABLISHED':
                raise ValueError
            numbers = [process['p'], process['u'], item['f']]
            if any(not re.fullmatch(r'0|[1-9][0-9]*', n) for n in numbers) or int(numbers[0]) <= 0:
                raise ValueError
            endpoints = item['n'].split('->')
            if len(endpoints) != 2:
                raise ValueError
            pair = []
            for endpoint in endpoints:
                host, port = endpoint.rsplit(':', 1)
                if str(ipaddress.IPv4Address(host)) != host or not re.fullmatch(r'[1-9][0-9]*', port) or int(port) > 65535:
                    raise ValueError
                pair.append((host, int(port)))
            records.append((*map(int, numbers), *pair))
        for row in raw.decode('ascii').splitlines():
            if len(row) < 2:
                raise ValueError
            key, value = row[0], row[1:]
            if key == 'p':
                if process and not item:
                    raise ValueError
                finish(); process, item = {'p': value}, {}
            elif key == 'f':
                finish(); item = {'f': value}
            elif key == 'u' and not item and key not in process:
                process[key] = value
            elif key in ('t', 'P', 'n', 'T') and item and key not in item:
                item[key] = value
            else:
                raise ValueError
        if not item:
            raise ValueError
        finish()
        return records
    except (ValueError, TypeError, UnicodeError):
        raise AuthRefused('connection_inventory_unqualified') from None


def status(token, *, binding=None, peer=None):
    """No proxy, redirects, body/log collection, URL credentials or child process."""
    if binding is None or peer is None:
        raise AuthRefused('listener_identity_unqualified')
    connection = http.client.HTTPConnection('127.0.0.1', 8000, timeout=2)
    try:
        binding()
        connection.connect()
        connection.auto_open = 0  # Never reconnect after the inspected socket disappears.
        sock = connection.sock
        endpoint = socket_identity(sock)
        binding()
        owner = peer(sock)
        def check_peer():
            if connection.sock is not sock or socket_identity(sock) != endpoint or peer(sock) != owner:
                raise AuthRefused('connected_peer_changed')
        check_peer()
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        connection.request('GET', '/v1/models', headers=headers)
        response = connection.getresponse()
        check_peer()
        return response.status
    except (OSError, http.client.HTTPException):
        return 0
    finally:
        connection.close()
        if binding is not None:
            binding()


def qualifies(token, *, request=status, revoked=None):
    if not isinstance(token, str) or not token:
        return False
    wrong = secrets.token_hex(32)
    return (request(token) == 200 and request(None) in (401, 403) and
            request(wrong) in (401, 403) and
            (not revoked or revoked == token or request(revoked) in (401, 403)))


class ManagedOmlx:
    """Only an already loaded, independently qualified, exact launchd job."""
    def __init__(self, prep, authorization, authorization_sha, source):
        raw = Path(authorization).read_bytes()
        if hashlib.sha256(raw).hexdigest() != authorization_sha:
            raise AuthRefused('authorization_mismatch')
        doc = json.loads(raw)
        expected = {'schema_version', 'source_commit', 'action', 'uid', 'plist_sha256', 'executable_sha256', 'runtime_manifest_sha256', 'native_qualified'}
        if (set(doc) != expected or type(doc['schema_version']) is not int or doc['schema_version'] != 1 or
                doc['source_commit'] != source or doc['action'] != 'local-auth' or
                type(doc['uid']) is not int or doc['uid'] != os.getuid() or doc['uid'] == 0 or
                doc['native_qualified'] is not True):
            raise AuthRefused('authorization_mismatch')
        self.prep = prep
        self.uid = doc['uid']
        self.path = Path.home() / 'Library/LaunchAgents/com.wisp.omlx.plist'
        self.raw = read_private(self.path)
        if hashlib.sha256(self.raw).hexdigest() != doc['plist_sha256']:
            raise AuthRefused('supervision_mismatch')
        plist = plistlib.loads(self.raw)
        argv = plist.get('ProgramArguments', [])
        executable = argv[0] if isinstance(argv, list) and argv else ''
        # Versioned immutable candidate layout produced by render_omlx.
        if not isinstance(executable, str) or not re.fullmatch(re.escape(str(Path.home())) +
                r'/Library/Application Support/Wisp/omlx/[0-9]+\.[0-9]+\.[0-9]+/bin/omlx', executable):
            raise AuthRefused('executable_unqualified')
        self.executable = executable
        # Exact qualified argv excludes secret flags, alternate ports and arbitrary commands.
        if (plist.get('Label') != 'com.wisp.omlx' or plist.get('ProgramArguments') !=
                [self.executable, 'serve', '--host', '127.0.0.1', '--port', '8000'] or
                plist.get('EnvironmentVariables', {}) or plist.get('Program') or
                plist.get('StandardOutPath') != '/dev/null' or plist.get('StandardErrorPath') != '/dev/null'):
            raise AuthRefused('supervision_mismatch')
        self.executable_sha256 = doc['executable_sha256']
        self.runtime_manifest_sha256 = doc['runtime_manifest_sha256']
        if not re.fullmatch('[0-9a-f]{64}', self.runtime_manifest_sha256):
            raise AuthRefused('runtime_tree_unqualified')
        self.target = 'gui/' + str(os.getuid()) + '/com.wisp.omlx'
        self.check_loaded()

    def check_loaded(self):
        if read_private(self.path) != self.raw:
            raise AuthRefused('supervision_changed')
        executable = Path(self.executable)
        if runtime_manifest(executable.parent.parent) != self.runtime_manifest_sha256:
            raise AuthRefused('runtime_tree_changed')
        if executable.resolve(strict=True) != executable:
            raise AuthRefused('executable_unqualified')
        fd = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022 or info.st_size > 64 * 1024 * 1024:
                raise AuthRefused('executable_unqualified')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                if hashlib.sha256(stream.read()).hexdigest() != self.executable_sha256:
                    raise AuthRefused('executable_changed')
        finally:
            os.close(fd)
        raw = self.prep.run(['/bin/launchctl', 'print', self.target]).decode('utf-8')
        # launchctl has no stable JSON contract. Accept only the qualified text
        # fields; unknown/multiple argument or environment sections fail closed.
        arguments = re.findall(r'(?m)^\s*arguments = \{\n([^}]+)\}', raw)
        if len(arguments) != 1 or [line.strip() for line in arguments[0].splitlines() if line.strip()] != [
                self.executable, 'serve', '--host', '127.0.0.1', '--port', '8000']:
            raise AuthRefused('loaded_arguments_unqualified')
        headers = re.findall(r'(?m)^\s*(?:default |inherited )?environment =', raw)
        blocks = re.findall(r'(?m)^\s*(?:default |inherited )?environment = \{\n([^}]*)\}', raw)
        if len(headers) != len(blocks):
            raise AuthRefused('loaded_environment_unqualified')
        for block in blocks:
            rows = [line.strip() for line in block.splitlines() if line.strip()]
            if any(row != 'PATH => /usr/bin:/bin:/usr/sbin:/sbin' for row in rows):
                raise AuthRefused('loaded_environment_unqualified')
        for field, value in [('program', self.executable), ('stdout path', '/dev/null'), ('stderr path', '/dev/null')]:
            if re.findall(r'(?m)^\s*' + re.escape(field) + r' = ([^\n]+)$', raw) != [value]:
                raise AuthRefused('loaded_service_unqualified')
        pids = re.findall(r'(?m)^\s*pid = ([^\n]+)$', raw)
        if (len(pids) != 1 or not re.fullmatch(r'[1-9][0-9]*', pids[0])
                or re.findall(r'(?m)^\s*state = ([^\n]+)$', raw) != ['running']):
            raise AuthRefused('loaded_pid_unqualified')
        return int(pids[0])

    def binding(self, expected_pid=None):
        pid = self.check_loaded()
        if expected_pid is not None and pid != expected_pid:
            raise AuthRefused('listener_identity_changed')
        listeners = tcp_listeners()
        if listeners is None or [(addr, port) for addr, port in listeners if port == 8000] != [('127.0.0.1', 8000)]:
            raise AuthRefused('listener_identity_unqualified')
        raw = self.prep.run(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000', '-sTCP:LISTEN', '-Fpufn']).decode('ascii')
        rows = raw.splitlines()
        if (len(rows) != 4 or rows[:2] != ['p' + str(pid), 'u' + str(self.uid)]
                or not re.fullmatch(r'f[0-9]+', rows[2]) or rows[3] != 'n127.0.0.1:8000'):
            raise AuthRefused('listener_identity_unqualified')
        if self.check_loaded() != pid:
            raise AuthRefused('listener_identity_changed')
        return pid

    def restart(self):
        if read_private(self.path) != self.raw:
            raise AuthRefused('supervision_changed')
        self.binding()
        self.prep.run(['/bin/launchctl', 'kickstart', '-k', self.target])
        # Wait for socket readiness without sending any token. A changed PID
        # is allowed only here, and only when the approved job owns the socket.
        deadline = time.monotonic() + 20
        while True:
            try:
                self.binding()
                return
            except AuthRefused:
                if time.monotonic() >= deadline:
                    raise AuthRefused('restart_listener_unqualified') from None
                time.sleep(0.2)

    def verify(self, token, revoked=None):
        pid = self.binding()
        incarnation = process_identity(pid, self.uid)
        def request(value):
            return status(value, binding=lambda: self.binding(pid),
                          peer=lambda sock: self.connected_peer(sock, pid, incarnation))
        return qualifies(token, request=request, revoked=revoked)

    def connected_peer(self, sock, pid, incarnation):
        port = getattr(self, "port", 8000)
        endpoint = socket_identity(sock, port)
        client_fd, local, remote = endpoint
        self.binding(pid)
        if process_identity(pid, self.uid) != incarnation:
            raise AuthRefused('process_identity_changed')
        try:
            command = ['/usr/sbin/lsof', '-nP', '-a', '-iTCP:' + str(port),
                       '-sTCP:ESTABLISHED', '-FpufPtTn', '-Ts']
            raw = self.prep.run(command)
            owners = connection_owners(raw)
            servers = [row for row in owners if row[3:] == (remote, local)]
            clients = [row for row in owners if row[3:] == (local, remote)]
            if (len(servers) != 1 or servers[0][:2] != (pid, self.uid)
                    or len(clients) != 1 or clients[0][:3] != (os.getpid(), os.getuid(), client_fd)):
                raise AuthRefused('connected_peer_unqualified')
            # A second complete kernel-backed lsof snapshot must preserve both
            # endpoint owners before request bytes are released.
            if sorted(connection_owners(self.prep.run(command))) != sorted(owners):
                raise AuthRefused('connection_kernel_unqualified')
        except (OSError, UnicodeError):
            raise AuthRefused('connection_inspection_unavailable') from None
        self.binding(pid)
        if process_identity(pid, self.uid) != incarnation or socket_identity(sock, port) != endpoint:
            raise AuthRefused('connected_peer_changed')
        return incarnation, servers[0][2], endpoint



import subprocess

def _lsof_tcp_listeners(raw):
    """Parse complete lsof process/file records for TCP listeners."""
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > 1024 * 1024:
            raise ValueError
        listeners = []
        pid = uid = descriptor = None
        completed = 0
        for row in raw.decode('ascii').splitlines():
            if len(row) < 2:
                raise ValueError
            key, value = row[0], row[1:]
            if key == 'p':
                if ((pid is not None and (uid is None or descriptor is not None or completed == 0))
                        or not value.isdigit() or int(value) <= 0):
                    raise ValueError
                pid, uid = int(value), None
                completed = 0
            elif key == 'u':
                if pid is None or uid is not None or not value.isdigit():
                    raise ValueError
                uid = int(value)
            elif key == 'f':
                if pid is None or uid is None or descriptor is not None or not value.isdigit():
                    raise ValueError
                descriptor = int(value)
            elif key == 'n':
                if descriptor is None:
                    raise ValueError
                if value.startswith('['):
                    match = re.fullmatch(r'\[([^]]+)\]:([0-9]+)', value)
                    if not match:
                        raise ValueError
                    host, port = match.groups()
                else:
                    host, separator, port = value.rpartition(':')
                    if not separator or not host:
                        raise ValueError
                if not port.isdigit() or not 0 < int(port) < 65536:
                    raise ValueError
                if host != '*':
                    host = str(ipaddress.ip_address(host))
                listeners.append((host, int(port)))
                descriptor = None
                completed += 1
            else:
                raise ValueError
        if pid is None or uid is None or descriptor is not None or completed == 0:
            raise ValueError
        return listeners
    except (ValueError, UnicodeError):
        return None


def tcp_listeners():
    try:
        result = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-iTCP",
                                 "-sTCP:LISTEN", "-Fpufn"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                                env={"PATH": "/usr/bin:/bin:/usr/sbin", "LC_ALL": "C"})
        if result.returncode or result.stderr.strip():
            return None
        return _lsof_tcp_listeners(result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return None
