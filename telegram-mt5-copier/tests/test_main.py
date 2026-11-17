from copier.__main__ import single_instance


def test_only_one_copy_can_run(tmp_path):
    path = str(tmp_path / "copier.lock")
    first = single_instance(path)
    assert first is not None
    assert single_instance(path) is None          # a second copy is refused
    first.close()                                 # first copy exits
    again = single_instance(path)
    assert again is not None
    again.close()
