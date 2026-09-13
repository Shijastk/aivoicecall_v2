import pytest

from shuo.bluetooth.telephony import (
    FakeTelephonyObserver,
    TelephonySnapshot,
)


@pytest.mark.asyncio
async def test_fake_telephony_observer_requires_no_live_dbus():
    expected = TelephonySnapshot(
        gateway_path="/org/pipewire/Telephony/ag1",
        bluetooth_address="00:C7:11:7B:84:21",
        transport_state="active",
        codec_value=2,
        call_paths=(),
    )

    observer = FakeTelephonyObserver(expected)

    actual = await observer.snapshot()

    assert actual == expected
    assert observer.calls == 1