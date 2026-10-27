"""Wisp's local service.

A /resetwisp that committed (service/wisp_reset.py wrote ``.reset-pending``) but
was cut short is finished here: on the first import of anything in the service,
before any module opens a store or a database. Without that marker this is one
lstat and nothing else.
"""


def _finish_pending_reset() -> None:
    import os
    from service.paths import moe_dir
    home = moe_dir()
    if not os.path.lexists(home / ".reset-pending"):
        return
    from service import wisp_reset
    wisp_reset.resume_pending(home)


_finish_pending_reset()
