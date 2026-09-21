"""Synthetic compatibility checks for the independently updated oMLX.app."""
import os

from service.inference import attributed_transport


def test_missing_manifest_accepts_only_official_desktop_runtime(monkeypatch):
    executable = "/Applications/oMLX.app/Contents/Resources/Python/cpython/bin/python3.11"
    calls = []

    def inspect(argv):
        calls.append(argv)
        if "-iTCP:8000" in argv:
            return f"p321\nu{os.getuid()}\nf4\nn127.0.0.1:8000\n".encode()
        return ("p321\nftxt\nn" + executable + "\n").encode()

    class Info:
        st_mode = 0o100755
        st_uid = os.getuid()

    monkeypatch.setattr(attributed_transport, "inspect_command", inspect)
    monkeypatch.setattr(attributed_transport.Path, "resolve", lambda self, strict=False: self)
    monkeypatch.setattr(attributed_transport.Path, "stat", lambda self: Info())
    authority = attributed_transport.DesktopOmlx()
    assert authority.binding() == 321
    assert calls
