from __future__ import annotations

import asyncio

from app.main import bridge
from app.polling import run_polling


async def main() -> None:
    await bridge.startup()
    try:
        await run_polling(bridge.telegram, bridge.handle_update)
    finally:
        await bridge.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
