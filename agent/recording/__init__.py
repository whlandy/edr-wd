"""Desktop recording compilation and golden replay contracts."""

from .compiler import CompilationResult, compile_recording
from .artifacts import RecordingArtifacts, write_compilation_artifacts
from .models import (
    COMPILED_CASE_SCHEMA,
    GOLDEN_TRACE_SCHEMA,
    GoldenTrace,
    RecordedTestCase,
    ReplaySelector,
)

__all__ = [
    "COMPILED_CASE_SCHEMA", "GOLDEN_TRACE_SCHEMA", "CompilationResult",
    "GoldenTrace", "RecordedTestCase", "RecordingArtifacts", "ReplaySelector",
    "compile_recording", "write_compilation_artifacts",
]
