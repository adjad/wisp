#!/usr/bin/env python3
"""Connect Wisp to Gmail (read-only) so mail syncs without Mail.app open.

One-time setup in console.cloud.google.com: enable the Gmail API, configure the
OAuth consent screen (add yourself as a test user), then create an OAuth client
of type "Desktop app". Run:

    python scripts/gmail_connect.py <client_id> [client_secret]

Your browser opens for consent; the token is stored in ~/.moe/gmail.json (0600).
"""
import asyncio
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import gmail_source  # noqa: E402


async def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    url = await gmail_source.begin_authorization(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
    print("Opening browser for Google consent…\n", url)
    webbrowser.open(url)
    for _ in range(300):
        await asyncio.sleep(1)
        info = gmail_source.status()
        if not info["authorizing"]:
            if not info["connected"]:
                sys.exit("Google sign-in did not finish. Try again.")
            print("Connected accounts:", ", ".join(row["email"] for row in info["accounts"]))
            await asyncio.sleep(2)
            return
    sys.exit("Timed out waiting for consent.")


if __name__ == "__main__":
    asyncio.run(main())
