from pc_town import should_include, Delta


def test_excludes_named_system_processes():
    for n in ("svchost.exe", "System", "Registry", "", "Memory Compression", "csrss.exe"):
        assert not should_include(n, 100, 8), n


def test_excludes_kernel_children():
    # ppid == 4 = children of System (Registry, Memory Compression, ...)
    assert not should_include("weirdkernelthing.exe", 101, 4)


def test_excludes_self():
    assert not should_include("pythonw.exe", 7, 4000, own_pids={7})


def test_includes_user_apps():
    assert should_include("chrome.exe", 500, 1200)
    assert should_include("Code.exe", 501, 1200)  # case-insensitive


def test_delta_rate():
    d = Delta()
    assert d.update(100, 0.0) == 0.0          # round แรกยังไม่มี delta
    assert d.update(300, 2.0) == 100.0        # (300-100)/2s
    assert d.update(250, 3.0) == 0.0          # counter reset → ไม่ติดลบ
