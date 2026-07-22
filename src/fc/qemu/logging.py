# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the MIT License.  See the LICENSE file in the root of this
# repository for complete details.
# The ConsoleRenderer is based on structlog.dev.ConsoleRenderer


import os
import sys
from io import StringIO
from pathlib import Path

import structlog

try:
    import colorama
except ImportError:
    colorama = None

_MISSING = (
    "{who} requires the {package} package installed.  "
    "If you want to use the helpers from structlog.dev, it is strongly "
    "recommended to install structlog using `pip install structlog[dev]`."
)
_EVENT_WIDTH = 30  # pad the event name to so many characters


def _pad(s, target_length):
    """
    Pads *s* to length *l*.
    """
    missing = target_length - len(s)
    return s + " " * (missing if missing > 0 else 0)


class TTYCodes:
    def __init__(self, disabled=False):
        if not disabled and sys.stdout.isatty() and colorama:
            self.colorized_tty_output = True
            self.reset_all = colorama.Style.RESET_ALL
            self.bright = colorama.Style.BRIGHT
            self.dim = colorama.Style.DIM
            self.red = colorama.Fore.RED
            self.backred = colorama.Back.RED
            self.blue = colorama.Fore.BLUE
            self.cyan = colorama.Fore.CYAN
            self.magenta = colorama.Fore.MAGENTA
            self.yellow = colorama.Fore.YELLOW
            self.green = colorama.Fore.GREEN
        else:
            self.colorized_tty_output = False
            self.reset_all = ""
            self.bright = ""
            self.dim = ""
            self.red = ""
            self.backred = ""
            self.blue = ""
            self.cyan = ""
            self.magenta = ""
            self.yellow = ""
            self.green = ""


TTY_CODES = TTYCodes(disabled=bool(int(os.environ.get("FCQEMU_NO_TTY", 0))))


class MultiOptimisticLoggerFactory(object):
    def __init__(self, **factories):
        self.factories = factories

    def __call__(self, *args):
        loggers = {k: f() for k, f in list(self.factories.items())}
        return MultiOptimisticLogger(loggers)


class MultiOptimisticLogger(object):
    def __init__(self, loggers):
        self.loggers = loggers

    def __repr__(self) -> str:
        return "<MultiOptimisticLogger {}>".format(
            [repr(logger) for logger in self.loggers]
        )

    def msg(self, **event_dict):
        for name, logger in list(self.loggers.items()):
            try:
                line = event_dict.get(name)
                if line:
                    logger.msg(line)
            except Exception:
                # We're being really optimistic: we want the calling program
                # to continue even if we face huge troubles logging stuff.
                pass

    def __getattr__(self, name):
        return self.msg


def prefix(prefix, line):
    return "{}>\t".format(prefix) + line.replace("\n", "\n{}>\t".format(prefix))


def format_value(value):
    if isinstance(value, Path):
        value = str(value)
    return repr(value)


class MultiConsoleRenderer(object):
    """
    Render `event_dict` nicely aligned, in colors, and ordered with
    specific knowledge about fc.qemu structures.
    """

    LEVELS = [
        "exception",
        "critical",
        "error",
        "warn",
        "warning",
        "info",
        "debug",
    ]

    def __init__(self, min_level, pad_event=_EVENT_WIDTH):
        self.min_level = self.LEVELS.index(min_level.lower())
        if colorama is None:
            raise SystemError(
                _MISSING.format(who=self.__class__.__name__, package="colorama")
            )
        if TTY_CODES.colorized_tty_output:
            colorama.init()

        self._pad_event = pad_event
        self._level_to_color = {
            "critical": TTY_CODES.red,
            "exception": TTY_CODES.red,
            "error": TTY_CODES.red,
            "warn": TTY_CODES.yellow,
            "warning": TTY_CODES.yellow,
            "info": TTY_CODES.green,
            "debug": TTY_CODES.green,
            "notset": TTY_CODES.backred,
        }
        for key in list(self._level_to_color.keys()):
            self._level_to_color[key] += TTY_CODES.bright
        self._longest_level = len(
            max(self._level_to_color.keys(), key=lambda e: len(e))
        )

    def __call__(self, logger, method_name, event_dict):
        console_io = StringIO()
        log_io = StringIO()

        def write(line):
            console_io.write(line)
            if TTY_CODES.reset_all:
                for SYMB in [
                    TTY_CODES.reset_all,
                    TTY_CODES.bright,
                    TTY_CODES.dim,
                    TTY_CODES.red,
                    TTY_CODES.backred,
                    TTY_CODES.blue,
                    TTY_CODES.cyan,
                    TTY_CODES.magenta,
                    TTY_CODES.yellow,
                    TTY_CODES.green,
                ]:
                    line = line.replace(SYMB, "")
            log_io.write(line)

        ts = event_dict.pop("timestamp", None)
        if ts is not None:
            write(
                TTY_CODES.dim
                + str(ts)  # can be a number if timestamp is UNIXy
                + TTY_CODES.reset_all
                + " "
            )

        pid = event_dict.pop("pid", None)
        if pid is not None:
            write(TTY_CODES.dim + str(pid) + TTY_CODES.reset_all + " ")

        level = event_dict.pop("level", None)
        if level is not None:
            write(
                self._level_to_color[level]
                + level[0].upper()
                + TTY_CODES.reset_all
                + " "
            )

        machine = event_dict.pop("machine", "")
        if machine:
            write(machine.ljust(10) + " ")
        subsystem = event_dict.pop("subsystem", "")
        if machine or subsystem:
            # Always keep the columns in order if we have a machine.
            write(subsystem.rjust(10)[:10] + " ")

        output = event_dict.pop("output", None)
        output_line = event_dict.pop("output", None)
        args = event_dict.pop("args", None)
        stack = event_dict.pop("stack", None)
        exc = event_dict.pop("exception", None)

        event = event_dict.pop("event", None)
        if event:
            write(
                TTY_CODES.bright
                + _pad(event, self._pad_event)
                + TTY_CODES.reset_all
                + " "
            )

            logger_name = event_dict.pop("logger", None)
            if logger_name is not None:
                write(
                    "["
                    + TTY_CODES.blue
                    + TTY_CODES.bright
                    + logger_name
                    + TTY_CODES.reset_all
                    + "] "
                )

            write(
                " ".join(
                    TTY_CODES.cyan
                    + key
                    + TTY_CODES.reset_all
                    + "="
                    + TTY_CODES.magenta
                    + format_value(event_dict[key])
                    + TTY_CODES.reset_all
                    for key in sorted(event_dict.keys())
                )
            )

            if args is not None:
                write(
                    "\n"
                    + TTY_CODES.dim
                    + prefix(machine, event + " " + "".join(args))
                    + TTY_CODES.reset_all
                )

        if output_line:
            write(
                "\n"
                + TTY_CODES.dim
                + prefix(machine, output_line)
                + TTY_CODES.reset_all
            )

        if output is not None:
            write(
                "\n"
                + TTY_CODES.dim
                + prefix(machine, output)
                + TTY_CODES.reset_all
            )

        if stack is not None:
            write("\n" + prefix(machine, stack))
            if exc is not None:
                write("\n\n" + prefix(machine, "=" * 79 + "\n"))
        if exc is not None:
            write("\n" + prefix(machine, exc))

        # Filter according to the -v switch when outputting to the
        # console.
        if self.LEVELS.index(method_name.lower()) > self.min_level:
            console_io.seek(0)
            console_io.truncate()

        return {"console": console_io.getvalue(), "file": log_io.getvalue()}


def method_to_level(logger, method_name, event_dict):
    event_dict["level"] = method_name
    return event_dict


def add_pid(logger, method_name, event_dict):
    event_dict["pid"] = os.getpid()
    return event_dict


def init_logging(verbose=True, console_target=sys.stdout):
    log_file = open("/var/log/fc-qemu.log", "a")
    structlog.configure(
        processors=[
            method_to_level,
            add_pid,
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            MultiConsoleRenderer(min_level="debug" if verbose else "info"),
        ],
        logger_factory=MultiOptimisticLoggerFactory(
            console=structlog.PrintLoggerFactory(console_target),
            file=structlog.PrintLoggerFactory(log_file),
        ),
    )
