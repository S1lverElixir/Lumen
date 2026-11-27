from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "audit" / "engineering-audit.jsonl"
REPORT = ROOT / "docs" / "ENGINEERING_AUDIT.md"
REVISION = "4b5efc7665d3eed08e4cf11e193efd814762d355"


def python_locations(path: str, data: bytes) -> dict[tuple[str, str], list[int]]:
    tree = ast.parse(data.decode("utf-8-sig"), filename=path)
    found = {}

    def add(kind, name, node):
        found[(kind, name)] = [node.lineno, node.end_lineno]

    def add_constant(name, node):
        kind = "module_constant"
        if (kind, name) in found:
            name = f"{name}@{node.lineno}"
        add(kind, name, node)

    def visit(body, prefix, parent_kind):
        for node in body:
            if isinstance(node, ast.ClassDef):
                name = prefix + node.name
                add("class", name, node)
                visit(node.body, name + ".", "class")
            elif isinstance(node, ast.FunctionDef):
                name = prefix + node.name
                kind = "test_function" if path.startswith("tests/") and node.name.startswith("test_") else (
                    "method" if parent_kind == "class" else "function"
                )
                add(kind, name, node)
                visit(node.body, name + ".<locals>.", "function")
            elif isinstance(node, ast.AsyncFunctionDef):
                name = prefix + node.name
                kind = "test_function" if path.startswith("tests/") and node.name.startswith("test_") else (
                    "async_method" if parent_kind == "class" else "async_function"
                )
                add(kind, name, node)
                visit(node.body, name + ".<locals>.", "function")
            else:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt):
                        visit([child], prefix, parent_kind)
                    elif isinstance(child, ast.ExceptHandler):
                        visit(child.body, prefix, parent_kind)
                    elif isinstance(child, ast.match_case):
                        visit(child.body, prefix, parent_kind)

    visit(tree.body, "", "module")
    found[("module", "<module>")] = [1, len(data.decode("utf-8-sig").splitlines())]
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    add_constant(target.id, node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            add_constant(node.target.id, node)
    return found


rows = [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]
meta = [r for r in rows if r.get("kind") == "meta"]
continuation = [r for r in rows if r.get("kind") == "continuation"]
assert len(meta) == 1 and len(continuation) == 1
meta = meta[0]

baseline_files = [r for r in rows if r.get("kind") == "file" and r.get("snapshot") == "baseline"]
baseline_elements = [r for r in rows if r.get("kind") == "element" and r.get("snapshot") == "baseline"]
current_files = [r for r in rows if r.get("kind") == "file" and r.get("snapshot") == "working-tree"]
current_elements = [r for r in rows if r.get("kind") == "element" and r.get("snapshot") == "working-tree"]
inventory = baseline_files + baseline_elements + current_files + current_elements
ids = [r["id"] for r in inventory]
assert len(ids) == len(set(ids)), "duplicate inventory IDs"
assert all(r.get("hash") for r in inventory), "missing hash"
assert all(r.get("status") for r in inventory), "missing status"
assert all(r.get("decision") for r in inventory), "missing decision"
allowed_statuses = {"NOT_REVIEWED", "REVIEWED", "IMPROVEMENT_REQUIRED", "REFACTORED", "VERIFIED", "ACCEPTED_AS_IS", "DEFERRED_WITH_REASON"}
assert all(r["status"] in allowed_statuses for r in inventory), "invalid inventory status"
assert all(r["status"] != "NOT_REVIEWED" for r in baseline_files + baseline_elements), "completed review left baseline entries unreviewed"
assert all(r["status"] != "NOT_REVIEWED" for r in current_files + current_elements), "current I1 entry left unreviewed"

tracked = [p for p in subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0") if p]
assert tracked == [r["path"] for r in baseline_files], "tracked-file coverage mismatch"
assert len(baseline_elements) == meta["elements"]
assert dict(Counter(r["element_type"] for r in baseline_elements)) == meta["element_counts"]
assert len(current_files) == meta["current_inventory"]["files"]
assert len(current_elements) == meta["current_inventory"]["elements"]
assert dict(Counter(r["element_type"] for r in current_elements)) == meta["current_inventory"]["element_counts"]
assert sorted(r["path"] for r in current_files) == sorted(meta["current_inventory"]["changed_files"])
status_entries = [entry for entry in subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=ROOT).decode("utf-8").split("\0") if entry]
deleted_files = sorted(entry[3:] for entry in status_entries if entry.startswith(" D "))
assert meta["current_inventory"]["deleted_files"] == deleted_files, "deleted-file coverage mismatch"
assert all(path in {r["path"] for r in baseline_files} and path not in meta["current_inventory"]["changed_files"] for path in deleted_files)

for rec in baseline_files:
    data = subprocess.check_output(["git", "show", f"{REVISION}:{rec['path']}"], cwd=ROOT)
    assert hashlib.sha256(data).hexdigest() == rec["hash"], f"baseline hash mismatch: {rec['path']}"
for rec in current_files:
    data = (ROOT / rec["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == rec["hash"], f"working hash mismatch: {rec['path']}"
for rec in current_elements:
    data = (ROOT / rec["path"]).read_bytes()
    if rec["element_type"] == "markdown_section":
        lines = data.decode("utf-8-sig").splitlines(keepends=True)
        section = "".join(lines[rec["lines"]["start"] - 1:rec["lines"]["end"]]).encode("utf-8")
        assert hashlib.sha256(section).hexdigest() == rec["hash"], f"section hash mismatch: {rec['id']}"
    else:
        assert hashlib.sha256(data).hexdigest() == rec["hash"], f"element hash mismatch: {rec['id']}"

for snapshot, elements in (("baseline", baseline_elements), ("working-tree", current_elements)):
    location_cache = {}
    for rec in elements:
        if not rec["path"].endswith(".py"):
            continue
        if rec["path"] not in location_cache:
            data = (
                subprocess.check_output(["git", "show", f"{REVISION}:{rec['path']}"], cwd=ROOT)
                if snapshot == "baseline"
                else (ROOT / rec["path"]).read_bytes()
            )
            location_cache[rec["path"]] = python_locations(rec["path"], data)
        expected = location_cache[rec["path"]].get((rec["element_type"], rec["qualified_name"]))
        assert expected is not None, f"Python element missing from AST: {rec['id']}"
        recorded = rec["lines"]
        if isinstance(recorded, dict):
            recorded = [recorded["start"], recorded["end"]]
        assert recorded == expected, f"Python element line map mismatch: {rec['id']}"

python_files = [p for p in tracked if p.endswith(".py")]
modules = [r for r in baseline_elements if r["element_type"] == "module"]
assert sorted(python_files) == sorted(r["path"] for r in modules), "Python module coverage mismatch"
assert any(r["path"] == "lumen_commands.py" and r["qualified_name"] == "inline_draw.<locals>._sized_call" for r in baseline_elements), "nested function missing"
assert Counter(r["element_type"] for r in baseline_elements)["deno_test"] > 0
assert Counter(r["element_type"] for r in baseline_elements)["ts_function"] > 0

reviews = [r for r in rows if r.get("kind") == "review"]
reviewed_paths = {path for review in reviews if review.get("status") == "completed" for path in review.get("files", [])}
assert all(r["status"] == "NOT_REVIEWED" or r["path"] in reviewed_paths or r["status"] == "IMPROVEMENT_REQUIRED" for r in baseline_elements), "element marked reviewed outside completed review"
assert all(r["path"] in reviewed_paths for r in current_files), "current I1 file lacks completed review"
assert any(r.get("id") == "review-R10" and r.get("status") == "completed" for r in reviews)
assert any(r.get("id") == "review-I1" and r.get("verdict") == "fixed_verified" for r in reviews)
assert any(r.get("id") == "review-R11" and r.get("status") == "completed" for r in reviews)
assert any(
    r.get("id") == "review-I30-source-r3"
    and r.get("status") == "completed"
    and r.get("verdict") == "fixed_verified"
    for r in reviews
), "source-language correction cycle lacks fixed re-review"
assert any(
    r.get("id") == "review-FINAL-I30"
    and r.get("status") == "completed"
    and r.get("verdict") == "fixed_verified"
    for r in reviews
), "final post-I30 whole-project review lacks fixed verdict"

findings = [r for r in rows if r.get("kind") == "finding"]
assert len({f["id"] for f in findings}) == len(findings)
assert all(f.get("priority") in {"P0", "P1", "P2", "P3"} for f in findings)
assert all(f.get("status") in {"confirmed", "verified", "rejected", "deferred"} for f in findings)
assert all(f.get("deferral_reason") for f in findings if f.get("status") == "deferred"), "deferred finding lacks reason"
assert all(
    finding["status"] == "verified"
    for finding in findings
    if finding["id"].startswith("R9-")
), "R9 documentation finding not reconciled"
assert all(t in set(ids) for f in findings for t in f.get("target_ids", [])), "finding target missing"
assert all(
    location in set(ids)
    for finding in findings
    for location in finding.get("fix_locations", [])
    if location.startswith("working-tree::")
), "finding fix location missing"
assert all(
    element["replacement_id"] in set(ids)
    for element in baseline_elements
    if element.get("replacement_id")
), "replacement element missing"
assert all(f.get("reviewer_verdict") == "fixed_verified" for f in findings if f.get("status") == "verified")
assert continuation[0]["open_findings"] == sorted(f["id"] for f in findings if f.get("status") == "confirmed")
assert meta["confirmed_open_findings"] == len(continuation[0]["open_findings"])
assert meta["verified_findings"] == sum(1 for f in findings if f.get("status") == "verified")

report = REPORT.read_text(encoding="utf-8")
text = LEDGER.read_text(encoding="utf-8") + report
assert "\ufffd" not in text, "replacement character in audit artifacts"
assert not any(0x80 <= ord(ch) <= 0x9F for ch in text), "C1 control character in audit artifacts"
assert meta["revision"] in report
assert str(meta["elements"]) in report
assert f"{meta['current_inventory']['files']} files" in report
assert f"{meta['current_inventory']['elements']:,} elements" in report
assert "{files}" not in report and "{elements" not in report, "unrendered report placeholder"
assert "post-I30 whole-project review is in progress" not in report, "report still marks final review pending"
assert "(110 files, 5,729 elements)" not in report, "stale current inventory count"
assert "| `VERIFIED` | 5839 |" not in report, "stale current inventory status count"
assert "Thirty-seven findings are deferred" not in report, "stale deferred finding count"
assert str(len(current_files) + len(current_elements)) in report, "current record total missing from report"
report_finding_rows = re.findall(
    r"^\| ((?:R\d+-\d+|RG-\d+|DOC-\d+|I2-CAP-\d+)) \| (P[0-3]) \| (confirmed|verified|deferred|rejected) \|",
    report,
    flags=re.MULTILINE,
)
assert {fid for fid, _, _ in report_finding_rows} == {f["id"] for f in findings}, "report finding coverage mismatch"
assert all(
    {f["id"]: f["status"] for f in findings}[fid] == shown_status
    for fid, _, shown_status in report_finding_rows
), "report finding status differs from ledger"
print(json.dumps({
    "baseline_files": len(baseline_files), "baseline_elements": len(baseline_elements),
    "current_files": len(current_files), "current_elements": len(current_elements),
    "findings": len(findings), "open_findings": meta["confirmed_open_findings"],
    "validation": "passed",
}, ensure_ascii=False, indent=2))
