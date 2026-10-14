from collections import deque
from pc_town import select_procs, verify_kill, proc_role, TOP_N


def P(name, pid, cpu):  # helper สร้าง item
    return {"name": name, "pid": pid, "cpu": cpu, "memMB": 10.0}


def test_excludes_hidden():
    items = [P("chrome.exe", 1, 5), P("bad.exe", 2, 9)]
    out = select_procs(items, set(), {"bad.exe"}, TOP_N)
    assert [d["name"] for d in out] == ["chrome.exe"]


def test_pinned_goes_first_even_low_cpu():
    items = [P("hot.exe", 1, 90), P("pet.exe", 2, 0.1)]
    out = select_procs(items, {"pet.exe"}, set(), TOP_N)
    assert [d["name"] for d in out] == ["pet.exe", "hot.exe"]  # pinned เรียงก่อน


def test_pinned_dead_process_drops_out():
    items = [P("hot.exe", 1, 90)]
    out = select_procs(items, {"ghost.exe"}, set(), TOP_N)
    assert [d["name"] for d in out] == ["hot.exe"]


def test_fill_order_by_cpu_and_cap():
    items = [P("a", 1, 1), P("b", 2, 50), P("c", 3, 30)]
    out = select_procs(items, set(), set(), 2)
    assert [d["name"] for d in out] == ["b", "c"]


def test_verify_kill():
    procs = [P("x.exe", 42, 1)]
    assert verify_kill(procs, "x.exe", 42) == "ok"
    assert verify_kill(procs, "x.exe", 99) == "gone"      # pid ไม่อยู่ (ตายไปแล้ว)
    assert verify_kill(procs, "y.exe", 42) == "mismatch"  # pid อยู่แต่ชื่อคนละตัว


def test_proc_role_type():
    assert proc_role(["C:\\app\\msedge.exe", "--type=renderer"]) == "renderer"
    assert proc_role(["C:\\app\\Code.exe", "--type=extensionHost"]) == "extensionhost"
    assert proc_role(["python.exe", "-c", "x"]) is None


def test_proc_role_script():
    assert proc_role(["python.exe", "burn.py"]) == "burn.py"
    assert proc_role(["python.exe"]) is None
