from pathlib import Path

from agent.recording.replay import load_golden_trace, replay_golden_trace


def test_hisec_logcenter(edr_wd_target):
    case_dir = Path(__file__).parent
    golden = load_golden_trace(case_dir / "golden-trace.json")
    result = replay_golden_trace(edr_wd_target, golden)
    assert result.task_success, result.summary
