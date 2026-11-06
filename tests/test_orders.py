from kaptyorka.orders import sign, signing_key, verify


def test_sign_and_verify_roundtrip():
    key = signing_key("s3cret")
    o = sign({"id": 1, "kind": "launch", "issued": 1000.0, "task": "x"}, key)
    assert verify(o, key, now=1001.0, seen_ids=()) is None


def test_changed_field_fails():
    key = signing_key("s3cret")
    o = sign({"id": 1, "kind": "launch", "issued": 1000.0, "task": "x"}, key)
    assert verify({**o, "task": "rm -rf"}, key, 1001.0, ()) == "bad signature"


def test_other_key_fails():
    o = sign({"id": 1, "issued": 1000.0}, signing_key("a"))
    assert verify(o, signing_key("b"), 1001.0, ()) == "bad signature"


def test_old_and_replayed_orders_are_refused():
    key = signing_key("s")
    o = sign({"id": 7, "issued": 1000.0}, key)
    assert verify(o, key, 1000.0 + 601, ()) == "order too old"
    assert verify(o, key, 1001.0, seen_ids={7}) == "order already run"


def test_malformed_order():
    assert verify({"id": 1}, signing_key("s"), 0.0, ()) == "malformed order"


def test_key_order_does_not_matter():
    key = signing_key("s")
    o = sign({"b": 1, "a": 2, "id": 1, "issued": 1.0}, key)
    assert verify(dict(reversed(list(o.items()))), key, 1.0, ()) is None


def test_non_dict_and_bad_field_types_are_malformed():
    key = signing_key("s")
    for bad in (None, [1, 2], "order", 3):
        assert verify(bad, key, 0.0, ()) == "malformed order"
    for issued in ("1000", None, True, [1]):
        o = sign({"id": 1, "issued": issued}, key)
        assert verify(o, key, 1000.0, ()) == "malformed order"
    o = sign({"id": [1], "issued": 1000.0}, key)
    assert verify(o, key, 1000.0, set()) == "malformed order"


def test_future_dated_order_counts_as_too_old():
    key = signing_key("s")
    o = sign({"id": 1, "issued": 1000.0 + 601}, key)
    assert verify(o, key, 1000.0, ()) == "order too old"
    assert verify(sign({"id": 2, "issued": 1000.0 + 60}, key), key, 1000.0, ()) is None
