"""Process supervisor for deployments whose credentials must remain in memory only.

Launch with an injected environment. Never reads or writes credential files.
Keeps the environment across child restarts; a host restart requires reinjection.
"""
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

os.umask(0o077)
Path('data').mkdir(exist_ok=True)
lock = open('data/supervisor.lock', 'w')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
Path('data/supervisor.pid').write_text(str(os.getpid()))
stopping = False
child = None


def stop(signum, frame):
    global stopping
    stopping = True
    if child and child.poll() is None:
        child.send_signal(signal.SIGINT)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while not stopping:
    child = subprocess.Popen([sys.executable, '-m', 'quantbot.main'])
    child.wait()
    if not stopping:
        time.sleep(5)
Path('data/supervisor.pid').unlink(missing_ok=True)
