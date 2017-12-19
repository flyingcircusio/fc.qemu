# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the MIT License.  See the LICENSE file in the root of this
# repository for complete details.
# The ConsoleRenderer is based on structlog.dev.ConsoleRenderer

from __future__ import absolute_import, division, print_function
from six import StringIO
import structlog
import sys
import os

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


def _pad(s, l):
    """
    Pads *s* to length *l*.
    """
    missing = l - len(s)
    return s + " " * (missing if missing > 0 else 0)


if sys.stdout.isatty() and colorama:
    RESET_ALL = colorama.Style.RESET_ALL
    BRIGHT = colorama.Style.BRIGHT
    DIM = colorama.Style.DIM
    RED = colorama.Fore.RED
    BACKRED = colorama.Back.RED
    BLUE = colorama.Fore.BLUE
    CYAN = colorama.Fore.CYAN
    MAGENTA = colorama.Fore.MAGENTA
    YELLOW = colorama.Fore.YELLOW
    GREEN = colorama.Fore.GREEN
else:
    RESET_ALL = ''
    BRIGHT = ''
    DIM = ''
    RED = ''
    BACKRED = ''
    BLUE = ''
    CYAN = ''
    MAGENTA = ''
    YELLOW = ''
    GREEN = ''


class MultiOptimisticLoggerFactory(object):

    def __init__(self, **factories):
        self.factories = factories

    def __call__(self, *args):
        loggers = {k: f() for k, f in self.factories.items()}
        return MultiOptimisticLogger(loggers)


class MultiOptimisticLogger(object):

    def __init__(self, loggers):
        self.loggers = loggers

    def __repr__(self):
        return '<MultiOptimisticLogger {}>'.format(
            [repr(l) for l in self.loggers])

    def msg(self, **event_dict):
        for name, logger in self.loggers.items():
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
    return '{}>\t'.format(prefix) + line.replace(
        '\n', '\n{}>\t'.format(prefix))


class MultiConsoleRenderer(object):
    """
    Render `event_dict` nicely aligned, in colors, and ordered with
    specific knowledge about fc.qemu structures.
    """

    LEVELS = ['exception', 'critical', 'error', 'warn', 'warning',
              'info', 'debug']

    def __init__(self, min_level, pad_event=_EVENT_WIDTH):
        self.min_level = self.LEVELS.index(min_level.lower())
        if colorama is None:
            raise SystemError(
                _MISSING.format(
                    who=self.__class__.__name__,
                    package="colorama"
                )
            )
        if sys.stdout.isatty():
            colorama.init()

        self._pad_event = pad_event
        self._level_to_color = {
            "critical": RED,
            "exception": RED,
            "error": RED,
            "warn": YELLOW,
            "warning": YELLOW,
            "info": GREEN,
            "debug": GREEN,
            "notset": BACKRED,
        }
        for key in self._level_to_color.keys():
            self._level_to_color[key] += BRIGHT
        self._longest_level = len(max(
            self._level_to_color.keys(),
            key=lambda e: len(e)
        ))

    def __call__(self, logger, method_name, event_dict):
        console_io = StringIO()
        log_io = StringIO()

        def write(line):
            console_io.write(line)
            if RESET_ALL:
                for SYMB in [RESET_ALL, BRIGHT, DIM, RED, BACKRED,
                             BLUE, CYAN, MAGENTA, YELLOW, GREEN]:
                    line = line.replace(SYMB, '')
            log_io.write(line)

        ts = event_dict.pop("timestamp", None)
        if ts is not None:
            write(
                # can be a number if timestamp is UNIXy
                DIM + str(ts) + RESET_ALL + " ")

        pid = event_dict.pop("pid", None)
        if pid is not None:
            write(
                DIM + str(pid) + RESET_ALL + " ")

        level = event_dict.pop("level", None)
        if level is not None:
            write(self._level_to_color[level] + level[0].upper() +
                  RESET_ALL + ' ')

        machine = event_dict.pop('machine', '')
        if machine:
            write(machine.ljust(20) + ' ')

        event = event_dict.pop("event")
        write(BRIGHT +
              _pad(event, self._pad_event) +
              RESET_ALL + " ")

        logger_name = event_dict.pop("logger", None)
        if logger_name is not None:
            write("[" + BLUE + BRIGHT +
                  logger_name + RESET_ALL +
                  "] ")

        output = event_dict.pop("output", None)
        args = event_dict.pop("args", None)
        stack = event_dict.pop("stack", None)
        exc = event_dict.pop("exception", None)
        write(" ".join(CYAN + key + RESET_ALL +
                       "=" +
                       MAGENTA + repr(event_dict[key]) +
                       RESET_ALL
                       for key in sorted(event_dict.keys())))

        if args is not None:
            write('\n' + DIM +
                  prefix(machine, event + ' ' + ''.join(args)) +
                  RESET_ALL)
        if output is not None:
            write('\n' + DIM + prefix(machine, output) + RESET_ALL)

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

        return {'console': console_io.getvalue(), 'file': log_io.getvalue()}


def method_to_level(logger, method_name, event_dict):
    event_dict['level'] = method_name
    return event_dict


def add_pid(logger, method_name, event_dict):
    event_dict['pid'] = os.getpid()
    return event_dict


def init_logging(verbose=True):
    log_file = open('/var/log/fc-qemu.log', 'a')
    structlog.configure(
        processors=[
            method_to_level,
            add_pid,
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt='iso', utc=False),
            MultiConsoleRenderer(min_level='debug' if verbose else 'info')
        ],
        logger_factory=MultiOptimisticLoggerFactory(
            console=structlog.PrintLoggerFactory(),
            file=structlog.PrintLoggerFactory(log_file))
    )
