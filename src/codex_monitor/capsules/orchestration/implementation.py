"""Permanent rejection of the retired usage-monitor orchestration surface."""
import json


class C2OrchestratorError(RuntimeError):
    pass


def _retired(*args, **kwargs):
    raise C2OrchestratorError('retired: lifecycle and recovery belong to codex-roadmap/C3')


claim_prompt = finish_prompt = checkpoint_next_action = global_checkpoint = _retired
orchestrator_status = runnable_prompts = context_search = _retired


def main(argv=None):
    print(json.dumps({'status': 'blocked', 'error': 'retired: use codex-roadmap/C3 directly'}))
    return 2
