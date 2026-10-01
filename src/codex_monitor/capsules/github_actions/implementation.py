"""Retired global CI scan: usage telemetry is not Git/runtime authority."""
import json


class WatchError(RuntimeError):
    pass


def _retired(*args, **kwargs):
    raise WatchError('retired: Git/CI belongs to github-autosync; observed health to fedora-system-monitor')


notification_message = process_scan = publish_snapshot = semantic_snapshot = _retired


def main(argv=None):
    print(json.dumps({'status': 'blocked', 'error': 'retired: global CI scan is not usage telemetry'}))
    return 2
