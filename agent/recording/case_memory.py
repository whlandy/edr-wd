"""Local, rebuildable trajectory case memory; no model or GUI execution.

Immutable receipts reference source bytes. SQLite contains only a small,
allowlisted structural projection, never input text, screenshots or tokens.
Ready is not replay pass; reported pass is not independently verified live.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

SCHEMA = "edr.case-memory/v1"
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _codes(values):
    return sorted({v for v in values if isinstance(v, str) and _TOKEN.fullmatch(v)})


def _source_matches(raw, golden):
    from .models import canonical_json
    expected = "sha256:" + _digest(canonical_json(raw.to_dict()).encode())
    if golden.source_recording.get("sha256") != expected or golden.source_recording.get("schema") != raw.schema:
        raise ValueError("case_memory_raw_trace_mismatch")


def _immutable(path, data):
    """Publish a new receipt without replacing a concurrent/existing receipt."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("case_memory_immutable_conflict")
        return
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("case_memory_immutable_conflict")
    finally:
        os.unlink(temp)


class CaseMemory:
    def __init__(self, record_root):
        self.root = Path(record_root).expanduser().resolve()
        self.home = self.root / "case-memory"
        if self.root not in self.home.resolve().parents:
            raise ValueError("case_memory_directory_escape")

    @contextmanager
    def _db(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.home / "catalog.sqlite"
        # A knowledge index must not make evidence world-readable.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(str(path), timeout=5)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, case_id TEXT NOT NULL, payload TEXT NOT NULL)")
            yield db
            db.commit()
        finally:
            db.close()

    def _resolve(self, reference):
        rel = Path(reference["path"])
        path = (self.root / rel).resolve()
        if rel.is_absolute() or self.root not in path.parents:
            raise ValueError("case_memory_reference_escape")
        return path

    def _check(self, references):
        for ref in references.values():
            if _digest(self._resolve(ref).read_bytes()) != ref["sha256"]:
                raise ValueError("case_memory_source_changed")

    def _freeze(self, path):
        path = Path(path).resolve()
        if self.root not in path.parents:
            raise ValueError("case_memory_source_outside_record_root")
        data = path.read_bytes()
        digest = _digest(data)
        # Reuse immutable v3 originals. Flat compatibility outputs can be
        # overwritten on the next same-name recording: preserve those bytes.
        if self.root / "trajectories" not in path.parents:
            path = self.home / "objects" / (digest + ".json")
            _immutable(path, data)
        return {"path": path.relative_to(self.root).as_posix(), "sha256": digest}

    def preview(self, *, raw_path, trace_path, report_path):
        """Read/validate explicit inputs without creating index or snapshots."""
        from target.recording.models import RawRecording
        from .models import GoldenTrace
        paths = {"raw": Path(raw_path).resolve(), "trace": Path(trace_path).resolve(),
                 "report": Path(report_path).resolve()}
        for path in paths.values():
            if self.root not in path.parents:
                raise ValueError("case_memory_source_outside_record_root")
        raw = RawRecording.from_dict(json.loads(paths["raw"].read_text(encoding="utf-8")))
        golden = GoldenTrace.from_dict(json.loads(paths["trace"].read_text(encoding="utf-8")))
        _source_matches(raw, golden)
        report = json.loads(paths["report"].read_text(encoding="utf-8"))
        if report.get("status") != golden.status:
            raise ValueError("case_memory_compile_status_mismatch")
        return {"status": "dry_run", "files": {k: _digest(p.read_bytes()) for k, p in paths.items()}}

    def register(self, *, raw_path, trace_path, report_path):
        """Register persisted compilation facts, including empty/incomplete runs."""
        from target.recording.models import RawRecording
        from .models import GoldenTrace

        raw = RawRecording.from_dict(json.loads(Path(raw_path).read_text(encoding="utf-8")))
        golden = GoldenTrace.from_dict(json.loads(Path(trace_path).read_text(encoding="utf-8")))
        _source_matches(raw, golden)
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if report.get("status") != golden.status:
            raise ValueError("case_memory_compile_status_mismatch")
        refs = {"raw": self._freeze(raw_path), "trace": self._freeze(trace_path),
                "compileReport": self._freeze(report_path)}
        source_map = Path(trace_path).parent / "source-map.json"
        if source_map.is_file():
            refs["sourceMap"] = self._freeze(source_map)
        case_id = "CASE-" + _digest(_json({
            "session": raw.session_id, "raw": refs["raw"]["sha256"],
            "trace": refs["trace"]["sha256"],
            "report": refs["compileReport"]["sha256"],
        }).encode())
        backends = _codes(e.scope.backend for e in raw.events)
        # Product identity is a context key, not a live binding or inferred
        # product version. Unknown version is intentionally explicit.
        processes = sorted({e.scope.process_name or "" for e in raw.events})
        steps = []
        for step in golden.steps:
            selector = step.selector
            control = selector.control if selector else {}
            types = _codes([(control or {}).get("controlType")])
            steps.append({"stepId": step.step_id, "actionId": step.action_id,
                          "controlType": types[0] if types else None,
                          "issues": _codes(step.issues)})
        diagnostics = raw.capture_diagnostics
        counters = {k: diagnostics[k] for k in (
            "droppedPackets", "correlationErrorCount", "recorderUiEvents",
            "unidentifiedTargetEvents", "outOfScopeEvents")
            if isinstance(diagnostics.get(k), int) and not isinstance(diagnostics[k], bool)}
        issues = _codes(x.get("code") for x in report.get("issues", []) if isinstance(x, dict))
        capture_issues = _codes(x.get("code") for x in report.get("artifactIssues", []) if isinstance(x, dict))
        projection = {
            "schema": SCHEMA, "caseId": case_id, "refs": refs,
            "context": {"backends": backends,
                        "productKey": _digest(_json(processes).encode()) if processes else "unknown",
                        "productVersion": "unknown", "inputOrigin": "unknown"},
            "capture": {"eventCount": len(raw.events), "counters": counters,
                        "completeness": "unknown"},
            "collection": {"issues": capture_issues,
                           "status": "gaps" if capture_issues else "not_independently_verified"},
            "compile": {"status": golden.status, "issues": issues},
            "steps": steps,
            "replay": "not_run", "business": "not_verified",
        }
        # Explicitly no raw name/labels/text/input args/window handles in index.
        self._check(refs)
        _immutable(self.home / "journal" / "cases" / (case_id + ".json"),
                   _json(projection).encode())
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO cases VALUES (?, ?)", (case_id, _json(projection)))
        return {"status": "indexed", "caseId": case_id,
                "catalog": str(self.home / "catalog.sqlite")}

    def record_run(self, *, trace_path, summary_path):
        """Link persisted run evidence. Never mark live/business verified from a receipt."""
        trace_path, summary_path = Path(trace_path).resolve(), Path(summary_path).resolve()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(summary.get("runId"), str) or not summary["runId"]:
            raise ValueError("case_memory_run_id_missing")
        digest = _digest(trace_path.read_bytes())
        # Require the exact registered revision, not a guessed current pointer.
        with self._db() as db:
            cases = [json.loads(row[0]) for row in db.execute("SELECT payload FROM cases")]
        matched = [c for c in cases if c["refs"]["trace"]["sha256"] == digest
                   and self._resolve(c["refs"]["trace"]) == trace_path]
        if len(matched) != 1:
            raise ValueError("case_memory_run_case_ambiguous_or_missing")
        case = matched[0]
        self._check(case["refs"])
        if (trace_path.parents[2] / "runs") not in summary_path.parents:
            raise ValueError("case_memory_run_outside_trajectory")
        outcome = summary.get("outcome")
        if outcome not in {"pass", "fail", "unknown", "blocked", "skipped"}:
            outcome = "unknown"
        if outcome == "pass" and not (summary.get("ok") is True and summary.get("latched") is False):
            outcome = "unknown"
        ref = self._freeze(summary_path)
        run_key = "RUNREF-" + _digest(_json([case["caseId"], summary["runId"]]).encode())
        payload = {"schema": SCHEMA, "runRef": run_key, "caseId": case["caseId"],
                   "summary": ref, "outcome": outcome,
                   "evidenceKind": "reported_run", "business": "not_independently_verified"}
        _immutable(self.home / "journal" / "runs" / (run_key + ".json"), _json(payload).encode())
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?)",
                       (run_key, case["caseId"], _json(payload)))
        return {"status": "indexed", "caseId": case["caseId"], "runRef": run_key}

    def search(self, query="", *, limit=5, similar_to=None):
        """Deterministic structural retrieval, with source freshness checks."""
        if not 1 <= limit <= 20:
            raise ValueError("case_memory_limit_out_of_range")
        if not (self.home / "catalog.sqlite").is_file():
            return []
        with self._db() as db:
            cases = [json.loads(r[0]) for r in db.execute("SELECT payload FROM cases ORDER BY id")]
            runs = [json.loads(r[0]) for r in db.execute("SELECT payload FROM runs ORDER BY id")]
        origin = next((c for c in cases if c["caseId"] == similar_to), None)
        if similar_to is not None and origin is None:
            raise ValueError("case_memory_case_missing")
        terms = set(query.lower().split())
        def tags_of(case):
            tags = set(case["compile"]["issues"] + case["collection"]["issues"])
            tags.update(s["actionId"] for s in case["steps"] if s["actionId"])
            tags.update(s["controlType"] for s in case["steps"] if s["controlType"])
            return tags
        origin_tags = tags_of(origin) if origin else set()
        rows = []
        for case in cases:
            if case["caseId"] == similar_to:
                continue
            if origin is not None and case["context"] != origin["context"]:
                continue
            try:
                self._check(case["refs"])
            except (OSError, ValueError):
                continue  # stale evidence is not an eligible recommendation
            tags = tags_of(case)
            searchable = tags | {case["compile"]["status"]}
            score = sum(t in _json(sorted(searchable)).lower() for t in terms)
            if terms and not score:
                continue
            if origin is not None:
                if not tags.intersection(origin_tags):
                    continue
                score += len(tags & origin_tags) / max(1, len(tags | origin_tags))
            related = []
            for run in runs:
                if run["caseId"] != case["caseId"]:
                    continue
                try:
                    self._check({"summary": run["summary"]})
                except (OSError, ValueError):
                    continue
                related.append(run)
            rows.append({**case, "runs": related, "score": score,
                         "matchBasis": "structural_tags", "verifiedLiveReference": False})
        return sorted(rows, key=lambda c: (-c["score"], c["caseId"]))[:limit]

    def compare(self, left, right):
        cases = {}
        # Lookup directly to avoid the display limit dropping a requested case.
        with self._db() as db:
            for case_id in (left, right):
                row = db.execute("SELECT payload FROM cases WHERE id=?", (case_id,)).fetchone()
                if row is None:
                    raise ValueError("case_memory_case_missing")
                cases[case_id] = json.loads(row[0])
        a, b = cases[left], cases[right]
        self._check(a["refs"])
        self._check(b["refs"])
        if a["context"] != b["context"]:
            raise ValueError("case_memory_context_mismatch")
        first = None
        for i in range(max(len(a["steps"]), len(b["steps"]))):
            x = a["steps"][i] if i < len(a["steps"]) else None
            y = b["steps"][i] if i < len(b["steps"]) else None
            if x != y:
                first = {"position": i + 1, "left": x, "right": y}
                break
        return {"left": left, "right": right, "alignment": "ordinal_only_not_causal",
                "firstStructuralDifference": first,
                "compile": {"left": a["compile"], "right": b["compile"]},
                "capture": {"left": a["capture"], "right": b["capture"]},
                "refs": {"left": a["refs"], "right": b["refs"]},
                "diagnosis": "hypothesis_only", "guiExecuted": False}

    def rebuild(self):
        """Reconstruct the cache from verified immutable journal receipts."""
        cases, runs = [], []
        for kind, out in (("cases", cases), ("runs", runs)):
            for path in sorted((self.home / "journal" / kind).glob("*.json")):
                doc = json.loads(path.read_text(encoding="utf-8"))
                if doc.get("schema") != SCHEMA:
                    raise ValueError("case_memory_unknown_schema")
                self._check(doc["refs"] if kind == "cases" else {"summary": doc["summary"]})
                out.append(doc)
        ids = {c["caseId"] for c in cases}
        if any(r["caseId"] not in ids for r in runs):
            raise ValueError("case_memory_orphan_run")
        with self._db() as db:
            db.execute("DELETE FROM runs")
            db.execute("DELETE FROM cases")
            db.executemany("INSERT INTO cases VALUES (?, ?)", ((c["caseId"], _json(c)) for c in cases))
            db.executemany("INSERT INTO runs VALUES (?, ?, ?)",
                           ((r["runRef"], r["caseId"], _json(r)) for r in runs))
        return {"status": "rebuilt", "cases": len(cases), "runs": len(runs)}


def register_artifacts(record_root, artifacts):
    """Optional hook: errors stay visible without rewriting execution truth."""
    if os.environ.get("EDR_GA_CASE_MEMORY", "1") == "0":
        return {"status": "disabled"}
    try:
        raw = artifacts.recording
        if artifacts.trajectory_root is not None:
            sources = list((artifacts.trajectory_root / "sources").glob("*/recording.json"))
            if len(sources) == 1:
                raw = sources[0]
        return CaseMemory(record_root).register(
            raw_path=raw, trace_path=artifacts.trace_v3_path or artifacts.golden_trace,
            report_path=artifacts.compile_report)
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError):
        # Do not echo exceptions: file names and malformed source text may
        # contain credentials. Existing compile artifacts remain available.
        return {"status": "error", "code": "case_memory_index_failed",
                "retry": "memory import after preserving source artifacts"}
