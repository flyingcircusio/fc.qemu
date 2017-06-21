from fc.qemu.logging import prefix, MultiOptimisticLoggerFactory
from structlog import PrintLoggerFactory


def test_prefix():
    assert prefix("test00", "asdfasdfasdfa") == """\
test00>\tasdfasdfasdfa\
"""
    assert prefix("test00", "first line\nsecond line\nthird line\n") == """\
test00>\tfirst line
test00>\tsecond line
test00>\tthird line
test00>\t"""


def test_multi_logger_ignores_error_on_fd(tmpdir):
    closed_stdout = open(str(tmpdir / 'stdout'), 'w')
    closed_stdout.close()
    f = MultiOptimisticLoggerFactory(console=PrintLoggerFactory(closed_stdout))
    logger = f()
    logger.msg(console='asdf')
