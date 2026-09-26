from arp_harvester.reaper import kill_tree, pid_alive, reap


def test_missing_pid_is_not_alive() -> None:
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_kill_tree_on_missing_pid_does_not_raise() -> None:
    assert kill_tree(0) is False
    reap([999_999_999])
