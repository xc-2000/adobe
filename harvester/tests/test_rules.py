from arp_harvester.rules import (
    batch_size,
    is_proxy_connect_failure,
    pause_at,
    should_pause_mint,
    should_skip_idle,
)


def test_pause_when_standing_pool_is_full() -> None:
    assert pause_at(8, 3) == 8
    assert should_pause_mint(8, 8, 3) is True
    assert should_pause_mint(7, 8, 3) is False
    assert should_pause_mint(0, 8, 3) is False


def test_pause_is_standing_pool_not_browsers() -> None:
    assert pause_at(8, 3) == 8
    assert pause_at(8, 3) != 3 + 8


def test_batch_fills_up_to_target() -> None:
    assert batch_size(8, 0, 8) == 8
    assert batch_size(8, 6, 8) == 2
    assert batch_size(8, 8, 8) == 0
    assert batch_size(8, 10, 8) == 0


def test_skip_idle_below_waterline() -> None:
    assert should_skip_idle(0, 3) is True
    assert should_skip_idle(2, 3) is True
    assert should_skip_idle(3, 3) is False


def test_proxy_connect_failure_trips_on_empty_response() -> None:
    assert is_proxy_connect_failure(RuntimeError("net::ERR_EMPTY_RESPONSE")) is True
    assert is_proxy_connect_failure(RuntimeError("lease proxy CONNECT failed")) is True
    assert is_proxy_connect_failure(RuntimeError("browser did not use the lease proxy")) is True
    assert is_proxy_connect_failure(RuntimeError("Sherlock SDK returned no ARP token")) is False
