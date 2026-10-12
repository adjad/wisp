"""``python -m brain_server --db ... --token-file ... [--ollama URL --model ID]``."""
from __future__ import annotations

import logging
import sys

from brain_server.config import ConfigError, load_token, parse_args


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = parse_args(argv)
        token = load_token(cfg.token_file)
    except ConfigError as exc:
        print(f"brain_server: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=cfg.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    phraser = None
    if cfg.ollama:
        from service.behavior.phraser import OpenAIPhraser
        # Native Ollama on loopback: OpenAI-compatible API under /v1, no token.
        phraser = OpenAIPhraser(cfg.ollama, api_prefix="/v1", model=cfg.model, token=None,
                                timeout=cfg.phraser_timeout)

    from brain_server.app import create_app
    import uvicorn

    app = create_app(cfg.db, token, phraser, tz=cfg.tz, background=cfg.background,
                     mine_interval_s=cfg.mine_interval_s, nudge_interval_s=cfg.nudge_interval_s)
    if not app.state.host.ready:
        logging.getLogger("brain_server").error("engine unavailable: %s", app.state.host.error)
    logging.getLogger("brain_server").info(
        "listening on http://%s:%d (db=%s, phraser=%s)", cfg.host, cfg.port, cfg.db,
        f"{cfg.ollama} {cfg.model}" if phraser else "templates only")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level,
                server_header=False, proxy_headers=False, timeout_keep_alive=5,
                limit_concurrency=64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
