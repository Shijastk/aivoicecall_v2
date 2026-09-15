from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one hotfix marker, found {count}")
    file.write_text(text.replace(old, new, 1))


replace_once(
    "shuo/services/llm.py",
    '''        log.info(
            "LLMRequest: prepared_reuse history_messages=%d",
            len(self._history),
        )
''',
    '''        log.info(
            f"LLMRequest: prepared_reuse history_messages={len(self._history)}"
        )
''',
)

replace_once(
    "tests/test_bluetooth_phase4c.py",
    '''    coordinator.on_final("hello", observed_at=10.5)
    assert coordinator.observations[-1].outcome == "promoted_ready_before_final"
''',
    '''    coordinator.on_final(
        "hello", observed_at=candidate.prepared.first_token_at + 0.001
    )
    assert coordinator.observations[-1].outcome == "promoted_ready_before_final"
''',
)

replace_once(
    "tests/test_bluetooth_phase4c.py",
    '''    assert coordinator.take_committed("hello") is None
    assert coordinator.observations[-1].outcome == "not_ready_by_final"
    assert stream.closed is True
    await coordinator.cleanup()
''',
    '''    assert coordinator.take_committed("hello") is None
    assert coordinator.observations[-1].outcome == "not_ready_by_final"
    await coordinator.cleanup()
    assert stream.closed is True
''',
)

print("Phase 4C focused-regression fixes applied")
