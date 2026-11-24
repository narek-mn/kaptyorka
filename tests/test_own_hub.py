"""paths.is_own_hub: the hub host is joined through its LAN address, not only localhost."""
import pytest

from kaptyorka import paths


def test_own_hub_by_the_machines_own_lan_address():
    ip = paths.outbound_ip()
    if ip.startswith("127."):
        pytest.skip("no LAN address here")
    assert paths.is_own_hub(f"http://{ip}:8765", {"port": 8765})  # hub.json without a host
    assert paths.is_own_hub("http://127.0.0.1:8765", {"port": 8765})
    assert not paths.is_own_hub(f"http://{ip}:9001", {"port": 8765})  # another port
    assert not paths.is_own_hub("http://192.0.2.1:8765", {"port": 8765})  # someone else's
