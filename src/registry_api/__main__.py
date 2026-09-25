"""Entry point: ``registry-api`` or ``python -m registry_api``."""

import uvicorn

from registry_api.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "registry_api.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
