import pytest

from shuo.bluetooth.codec import BLUETOOTH_PCM_FORMAT
from shuo.bluetooth.pipewire import PipeWireTarget
from shuo.bluetooth.process import CompletedCommand
from shuo.bluetooth.routes import (
    AiOnlyRouteIsolation,
    PipeWireLink,
    RouteIsolationError,
    find_ai_only_forbidden_links,
    parse_pw_link_listing,
)


ADDRESS = "00:C7:11:7B:84:21"
DOWNLINK_NODE = "bluez_input.00_C7_11_7B_84_21.0"
UPLINK_NODE = "bluez_output.00_C7_11_7B_84_21.1"
MIC_NODE = "alsa_input.pci-0000_00_1f.3.HiFi__Mic1__source"
SPEAKER_NODE = "alsa_output.pci-0000_00_1f.3.HiFi__Speaker__sink"


def _target(*, downlink: bool) -> PipeWireTarget:
    return PipeWireTarget(
        node_name=DOWNLINK_NODE if downlink else UPLINK_NODE,
        factory_name=(
            "api.bluez5.sco.source" if downlink else "api.bluez5.sco.sink"
        ),
        media_class=(
            "Stream/Output/Audio" if downlink else "Stream/Input/Audio"
        ),
        bluetooth_address=ADDRESS,
        bluetooth_profile="headset-audio-gateway",
        bluetooth_codec="msbc",
        audio_format=BLUETOOTH_PCM_FORMAT,
    )


PW_LINK_LISTING = f"""\
{SPEAKER_NODE}:playback_FL
  |<- Firefox:output_FL
  |<- {DOWNLINK_NODE}:output_FL
{SPEAKER_NODE}:playback_FR
  |<- Firefox:output_FR
  |<- {DOWNLINK_NODE}:output_FR
{MIC_NODE}:capture_FL
  |-> {UPLINK_NODE}:input_FL
{MIC_NODE}:capture_FR
  |-> {UPLINK_NODE}:input_FR
Firefox:output_FL
  |-> {SPEAKER_NODE}:playback_FL
Firefox:output_FR
  |-> {SPEAKER_NODE}:playback_FR
{DOWNLINK_NODE}:output_FL
  |-> {SPEAKER_NODE}:playback_FL
{DOWNLINK_NODE}:output_FR
  |-> {SPEAKER_NODE}:playback_FR
{UPLINK_NODE}:input_FL
  |<- {MIC_NODE}:capture_FL
{UPLINK_NODE}:input_FR
  |<- {MIC_NODE}:capture_FR
"""


def test_parse_pw_link_listing_deduplicates_both_endpoint_views():
    links = parse_pw_link_listing(PW_LINK_LISTING)

    assert PipeWireLink(
        f"{MIC_NODE}:capture_FL", f"{UPLINK_NODE}:input_FL"
    ) in links
    assert PipeWireLink(
        f"{DOWNLINK_NODE}:output_FL", f"{SPEAKER_NODE}:playback_FL"
    ) in links
    assert PipeWireLink(
        "Firefox:output_FL", f"{SPEAKER_NODE}:playback_FL"
    ) in links

    # 2 mic + 2 Bluetooth speaker + 2 Firefox speaker links.
    assert len(links) == 6


def test_find_ai_only_forbidden_links_leaves_unrelated_audio_untouched():
    links = parse_pw_link_listing(PW_LINK_LISTING)

    forbidden = find_ai_only_forbidden_links(
        links,
        downlink=_target(downlink=True),
        uplink=_target(downlink=False),
    )

    assert len(forbidden) == 4
    assert not any(link.output_port.startswith("Firefox:") for link in forbidden)


class GraphRunner:
    def __init__(self, links, *, fail_disconnect_at=None, fail_restore=False):
        self.links = set(links)
        self.calls = []
        self.fail_disconnect_at = fail_disconnect_at
        self.fail_restore = fail_restore
        self.disconnect_count = 0

    def _listing(self):
        by_output = {}
        by_input = {}
        for link in sorted(self.links):
            by_output.setdefault(link.output_port, []).append(link.input_port)
            by_input.setdefault(link.input_port, []).append(link.output_port)

        roots = sorted(set(by_output) | set(by_input))
        lines = []
        for root in roots:
            lines.append(root)
            for peer in sorted(by_output.get(root, [])):
                lines.append(f"  |-> {peer}")
            for peer in sorted(by_input.get(root, [])):
                lines.append(f"  |<- {peer}")
        return ("\n".join(lines) + "\n").encode()

    async def run(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)

        if argv == ("pw-link", "-l"):
            return CompletedCommand(argv, 0, self._listing(), b"")

        if argv[:2] == ("pw-link", "-d"):
            self.disconnect_count += 1
            if self.fail_disconnect_at == self.disconnect_count:
                return CompletedCommand(argv, 1, b"", b"disconnect failed")
            link = PipeWireLink(argv[2], argv[3])
            self.links.discard(link)
            return CompletedCommand(argv, 0, b"", b"")

        if argv[:1] == ("pw-link",) and len(argv) == 3:
            if self.fail_restore:
                return CompletedCommand(argv, 1, b"", b"restore failed")
            self.links.add(PipeWireLink(argv[1], argv[2]))
            return CompletedCommand(argv, 0, b"", b"")

        raise AssertionError(f"unexpected argv: {argv!r}")


@pytest.mark.asyncio
async def test_ai_only_start_removes_only_bluetooth_physical_routes_and_stop_restores():
    original = parse_pw_link_listing(PW_LINK_LISTING)
    runner = GraphRunner(original)
    isolation = AiOnlyRouteIsolation(
        downlink=_target(downlink=True),
        uplink=_target(downlink=False),
        runner=runner,
    )

    await isolation.start()

    assert isolation.running is True
    assert len(isolation.snapshot.removed_links) == 4

    # Firefox remains connected to the laptop speaker.
    assert PipeWireLink(
        "Firefox:output_FL", f"{SPEAKER_NODE}:playback_FL"
    ) in runner.links

    assert find_ai_only_forbidden_links(
        runner.links,
        downlink=_target(downlink=True),
        uplink=_target(downlink=False),
    ) == ()

    await isolation.stop()

    assert isolation.running is False
    assert isolation.snapshot.removed_links == ()
    assert runner.links == original


@pytest.mark.asyncio
async def test_already_isolated_graph_is_safe_and_idempotent():
    original = parse_pw_link_listing(PW_LINK_LISTING)
    safe = {
        link
        for link in original
        if link.output_port.startswith("Firefox:")
    }
    runner = GraphRunner(safe)
    isolation = AiOnlyRouteIsolation(
        downlink=_target(downlink=True),
        uplink=_target(downlink=False),
        runner=runner,
    )

    await isolation.start()
    await isolation.start()
    await isolation.stop()
    await isolation.stop()

    assert runner.links == safe


@pytest.mark.asyncio
async def test_partial_disconnect_failure_rolls_back_links_removed_by_this_start():
    original = parse_pw_link_listing(PW_LINK_LISTING)
    runner = GraphRunner(original, fail_disconnect_at=2)
    isolation = AiOnlyRouteIsolation(
        downlink=_target(downlink=True),
        uplink=_target(downlink=False),
        runner=runner,
    )

    with pytest.raises(RouteIsolationError):
        await isolation.start()

    assert isolation.running is False
    assert isolation.snapshot.removed_links == ()
    assert runner.links == original


@pytest.mark.asyncio
async def test_failed_restore_keeps_ownership_for_retry():
    original = parse_pw_link_listing(PW_LINK_LISTING)
    runner = GraphRunner(original)
    isolation = AiOnlyRouteIsolation(
        downlink=_target(downlink=True),
        uplink=_target(downlink=False),
        runner=runner,
    )

    await isolation.start()
    runner.fail_restore = True

    with pytest.raises(RouteIsolationError):
        await isolation.stop()

    assert isolation.snapshot.removed_links

    runner.fail_restore = False
    await isolation.stop()
    assert isolation.snapshot.removed_links == ()
    assert runner.links == original
