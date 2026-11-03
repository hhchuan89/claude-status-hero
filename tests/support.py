"""Keep test state, HOME and temporary files inside a disposable repo fixture."""
import atexit
import os
import sys
import tempfile


def isolate(root):
    sandbox = tempfile.TemporaryDirectory(prefix=".test-", dir=root)
    atexit.register(sandbox.cleanup)
    home = os.path.join(sandbox.name, "home")
    os.makedirs(home)
    sys.dont_write_bytecode = True
    os.environ.update(PYTHONDONTWRITEBYTECODE="1", HOME=home, USERPROFILE=home, TMPDIR=sandbox.name,
                      STATUS_HERO_DIR=os.path.join(home, "state"),
                      STATUS_HERO_EMOJI="0", STATUS_HERO_SIXEL="0")
    tempfile.tempdir = sandbox.name
