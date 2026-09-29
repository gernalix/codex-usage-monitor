from __future__ import annotations

import json

import codex_chat_dump_publisher as dumps


def line(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True) + "\n"


def test_append_rollout_is_incremental_and_preserves_full_redacted_records(tmp_path):
    session = "11111111-2222-3333-4444-555555555555"
    source = tmp_path / f"rollout-{session}.jsonl"
    repo = tmp_path / "repo"
    password_key = "pass" + "word"
    fine_grained_pat = "github_" + "pat_" + "A" * 40
    bearer = "Bearer " + "opaque-" + "B" * 32
    source.write_text(
        line({"type": "session_meta", "payload": {"id": session, "cwd": "/tmp/project"}})
        + line(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"{fine_grained_pat} {bearer}"}],
                    "metadata": {"kept": "yes", password_key: "fixture-value"},
                },
            }
        ),
        encoding="utf-8",
    )

    first = dumps.append_rollout(repo, source)
    assert first == {"changed": True, "records": 2}

    base = repo / "native-sessions" / session / "sources" / dumps.source_key(source)
    chunk1 = (base / "chunks/000001.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(chunk1) == 2
    record = json.loads(chunk1[1])
    assert record["payload"]["metadata"]["kept"] == "yes"
    assert record["payload"]["metadata"][password_key] == "[REDACTED]"
    published_text = record["payload"]["content"][0]["text"]
    assert fine_grained_pat not in published_text
    assert bearer not in published_text
    assert "[GITHUB_TOKEN_REDACTED]" in published_text
    assert "Bearer [REDACTED]" in published_text

    with source.open("a", encoding="utf-8") as handle:
        handle.write(line({"type": "event_msg", "payload": {"type": "task_complete", "extra": 7}}))
    second = dumps.append_rollout(repo, source)
    assert second == {"changed": True, "records": 1}
    assert (base / "chunks/000002.jsonl").is_file()

    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "partial"}}))
    partial = dumps.append_rollout(repo, source)
    assert partial == {"changed": False, "records": 0}

    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    final = dumps.append_rollout(repo, source)
    assert final == {"changed": True, "records": 1}
    assert (base / "chunks/000003.jsonl").is_file()

    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete_through_byte"] == source.stat().st_size
    assert manifest["chunk_count"] == 3
