import pytest

from shuo.bluetooth.codec import (
    AudioFormat,
    BLUETOOTH_PCM_FORMAT,
    SampleEncoding,
)
from shuo.bluetooth.pipewire import (
    PipeWireSelectionError,
    PipeWireTarget,
    StreamDirection,
    UnsupportedPlatformError,
    require_pipewire_platform,
    select_target,
)


ADDRESS = "00:C7:11:7B:84:21"


def _downlink(
    *,
    address: str = ADDRESS,
    node_name: str = "bluez_input.fixture.0",
) -> PipeWireTarget:
    return PipeWireTarget(
        node_name=node_name,
        factory_name="api.bluez5.sco.source",
        media_class="Stream/Output/Audio",
        bluetooth_address=address,
        bluetooth_profile="headset-audio-gateway",
        bluetooth_codec="msbc",
        audio_format=BLUETOOTH_PCM_FORMAT,
    )


def _uplink(
    *,
    address: str = ADDRESS,
    node_name: str = "bluez_output.fixture.1",
) -> PipeWireTarget:
    return PipeWireTarget(
        node_name=node_name,
        factory_name="api.bluez5.sco.sink",
        media_class="Stream/Input/Audio",
        bluetooth_address=address,
        bluetooth_profile="headset-audio-gateway",
        bluetooth_codec="msbc",
        audio_format=BLUETOOTH_PCM_FORMAT,
    )


def test_selects_downlink_by_capability_and_address():
    selected = select_target(
        [_downlink(), _uplink()],
        direction=StreamDirection.DOWNLINK,
        address=ADDRESS,
    )

    assert selected.factory_name == "api.bluez5.sco.source"


def test_selects_uplink_by_capability_and_address():
    selected = select_target(
        [_downlink(), _uplink()],
        direction=StreamDirection.UPLINK,
        address=ADDRESS,
    )

    assert selected.factory_name == "api.bluez5.sco.sink"


def test_rejects_wrong_codec():
    bad = PipeWireTarget(
        node_name="not-supported",
        factory_name="api.bluez5.sco.source",
        media_class="Stream/Output/Audio",
        bluetooth_address=ADDRESS,
        bluetooth_profile="headset-audio-gateway",
        bluetooth_codec="cvsd",
        audio_format=BLUETOOTH_PCM_FORMAT,
    )

    with pytest.raises(PipeWireSelectionError):
        select_target(
            [bad],
            direction=StreamDirection.DOWNLINK,
            address=ADDRESS,
        )


def test_rejects_wrong_audio_format():
    bad = PipeWireTarget(
        node_name="wrong-rate",
        factory_name="api.bluez5.sco.source",
        media_class="Stream/Output/Audio",
        bluetooth_address=ADDRESS,
        bluetooth_profile="headset-audio-gateway",
        bluetooth_codec="msbc",
        audio_format=AudioFormat(
            SampleEncoding.S16LE,
            8_000,
            1,
        ),
    )

    with pytest.raises(PipeWireSelectionError):
        select_target(
            [bad],
            direction=StreamDirection.DOWNLINK,
            address=ADDRESS,
        )


def test_ambiguous_device_selection_fails_closed():
    with pytest.raises(PipeWireSelectionError):
        select_target(
            [
                _downlink(address="AA:AA:AA:AA:AA:AA", node_name="phone-a"),
                _downlink(address="BB:BB:BB:BB:BB:BB", node_name="phone-b"),
            ],
            direction=StreamDirection.DOWNLINK,
        )


def test_reference_numeric_node_id_is_not_part_of_contract():
    target = _downlink()

    assert not hasattr(target, "node_id")


def test_linux_is_supported():
    require_pipewire_platform("Linux")


def test_unsupported_platform_fails_without_import_side_effects():
    with pytest.raises(UnsupportedPlatformError):
        require_pipewire_platform("Windows")