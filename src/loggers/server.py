from __future__ import annotations

from typing import TYPE_CHECKING

from hypercorn.logging import Logger as HypercornLogger

from .logger import Logger

if TYPE_CHECKING:
    from hypercorn.config import Config


class ServerLogger(HypercornLogger):
    """Use the application formatter for Hypercorn lifecycle and error logs."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.error_logger = Logger("hypercorn.error")
        self.error_logger.propagate = False
