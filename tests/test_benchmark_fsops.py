"""Benchmark and verification script for scan_output_tree optimization."""
import time
from unittest.mock import patch
from fermi_organizer import fsops
from fermi_organizer.runmodes import _collect_incremental_candidates, run_incremental


def create_mock_output_tree(tmp_path, num_organized=1500, num_sup=300, num_orph=300):
    output = tmp_path / "Output"
    output.mkdir(parents=True, exist_ok=True)

    # Organized tree files (nested)
    for i in range(num_organized):
        depth_dir = output / f"sub_{i % 20}" / f"sub2_{i % 50}"
        depth_dir.mkdir(parents=True, exist_ok=True)
        pdf = depth_dir / f"F10{i:05d}.pdf"
        pdf.write_bytes(b"%PDF-1.4 dummy")

    # Superseded files
    sup_dir = output / "_superseded"
    sup_dir.mkdir(parents=True, exist_ok=True)
    for i in range(num_sup):
        pdf = sup_dir / f"F10{i+20000:05d}.pdf"
        pdf.write_bytes(b"%PDF-1.4 dummy")

    # Orphan files
    orph_dir = output / "_orphans"
    orph_dir.mkdir(parents=True, exist_ok=True)
    for i in range(num_orph):
        pdf = orph_dir / f"F10{i+30000:05d}.pdf"
        pdf.write_bytes(b"%PDF-1.4 dummy")

    return output


def test_scan_output_tree_called_once_in_incremental_flow(tmp_path):
    output = create_mock_output_tree(tmp_path, num_organized=100, num_sup=20, num_orph=20)
    scan_index = {f"F10{i:05d}": output / f"input_F10{i:05d}.pdf" for i in range(10)}

    scan_count = 0
    original_scan = fsops.scan_output_tree

    def counting_scan(folder):
        nonlocal scan_count
        scan_count += 1
        return original_scan(folder)

    with patch("fermi_organizer.runmodes.scan_output_tree", side_effect=counting_scan), \
         patch.object(fsops, "scan_output_tree", side_effect=counting_scan):
        scan_count = 0
        scan_res = fsops.scan_output_tree(output)
        # scan_res passed explicitly
        fsops.find_organized_pdfs(output, scan_res=scan_res)
        fsops.retire_adopted_orphans({}, output, True, lambda msg: None, scan_res=scan_res)
        # Should only be the 1 explicit call above
        assert scan_count == 1, f"Expected 1 scan_output_tree call, got {scan_count}"

        scan_count = 0
        _collect_incremental_candidates(scan_index, output)
        assert scan_count == 1, f"Expected 1 scan_output_tree call in _collect_incremental_candidates, got {scan_count}"


def test_run_incremental_calls_scan_output_tree_once(tmp_path):
    output = create_mock_output_tree(tmp_path, num_organized=50, num_sup=10, num_orph=10)

    scan_count = 0
    original_scan = fsops.scan_output_tree

    def counting_scan(folder):
        nonlocal scan_count
        scan_count += 1
        return original_scan(folder)

    with patch("fermi_organizer.runmodes.scan_output_tree", side_effect=counting_scan), \
         patch.object(fsops, "scan_output_tree", side_effect=counting_scan):
        scan_count = 0
        run_incremental(output, output, dry_run=True, log=lambda msg: None)
        assert scan_count == 1, f"Expected 1 scan_output_tree call in run_incremental, got {scan_count}"


def benchmark_incremental_flow(tmp_path):
    output = create_mock_output_tree(tmp_path, num_organized=1500, num_sup=300, num_orph=300)

    scan_count = 0
    original_scan = fsops.scan_output_tree

    def counting_scan(folder):
        nonlocal scan_count
        scan_count += 1
        return original_scan(folder)

    iterations = 20
    last_scan_count = 0

    with patch("fermi_organizer.runmodes.scan_output_tree", side_effect=counting_scan), \
         patch.object(fsops, "scan_output_tree", side_effect=counting_scan):
        start_time = time.perf_counter()
        for _ in range(iterations):
            scan_count = 0
            run_incremental(output, output, dry_run=True, log=lambda msg: None)
            last_scan_count = scan_count
        total_time = time.perf_counter() - start_time

    avg_time_ms = (total_time / iterations) * 1000
    print("\n--- Benchmark Results ---")
    print(f"Iterations: {iterations}")
    print(f"Total calls to scan_output_tree per incremental run: {last_scan_count}")
    print(f"Avg execution time per run: {avg_time_ms:.3f} ms")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        benchmark_incremental_flow(Path(td))
