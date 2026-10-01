"""Retired duplicate observer: Fedora System Monitor owns runtime health."""
import json


class C2HealthError(RuntimeError):
    pass


def main(argv=None):
    print(json.dumps({'status': 'blocked', 'error': 'retired: runtime health belongs to fedora-system-monitor'}))
    return 2
