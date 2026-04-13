import logging
import re
import html

_ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

__all__ = ['Logger']

class _TelegramLogger(logging.Handler):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.ansi_escape = _ANSI_ESCAPE

    def emit(self, record):
        try:
            msg_text = self.format(record)
            clean_text = self.ansi_escape.sub('', msg_text)
            safe_text = html.escape(clean_text)
            self.bot.msg(f"<code>{safe_text}</code>")
        except Exception:
            self.handleError(record)

class Logger(logging.Logger):
    COLORS = {
        'DEBUG': '\033[36m',
        'INFO': '\033[32m',
        'WARNING': '\033[33m',
        'ERROR': '\033[31m',
        'CRITICAL': '\033[35m',
    }
    RESET = '\033[0m'

    def __init__(self, name: str, level=logging.DEBUG, bot = None):
        super().__init__(name, level)
        self.ansi_escape = _ANSI_ESCAPE
        if not self.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(self._make_formatter())
            self.addHandler(handler)
        
    def _make_formatter(self):
        parent = self
        class Fmt(logging.Formatter):
            def format(self, record):
                orig_level = record.levelname
                color = parent.COLORS.get(record.levelname, parent.RESET)
                record.levelname = f"{color}{record.levelname}{parent.RESET}"
                val = super().format(record)
                record.levelname = orig_level
                return val
        return Fmt(
            fmt='%(asctime)s %(levelname)s [%(name)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M'
        )
    def set_tg_bot(self, bot):
        tg_handler = _TelegramLogger(bot)
        tg_handler.setLevel(logging.INFO) 
        simple_fmt = logging.Formatter('%(levelname)s [%(name)s] %(message)s')
        tg_handler.setFormatter(simple_fmt)
        self.addHandler(tg_handler)
