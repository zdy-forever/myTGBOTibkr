"""Audit fingerprint of the strategy: store it with every strategy run so a signal can always be traced to the exact code and settings that produced it."""
import hashlib
import json

from . import config

_FILES = ('logic.py', 'universe_data.py', 'engine.py', 'signals.py', 'sec_fundamentals.py', 'pool.py')


def strategy_version():
    h = hashlib.sha256()
    for name in _FILES:
        h.update((config.HERE / name).read_bytes())
    h.update(json.dumps(config.STRATEGY, sort_keys=True).encode())
    return h.hexdigest()[:12]


def strategy_info():
    return dict(name=config.STRATEGY_NAME, version=strategy_version(), config=dict(config.STRATEGY), source='research repo: QuantConnect port, R5 + K1A + K5x')
