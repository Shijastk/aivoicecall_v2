"""
The voice catalogue -- the backend's answer to "which voices exist".

Closes open question 11. The panel's picker was four page-local fixtures
(`el-rachel-v2`, `el-adam-turbo`, `oa-alloy`, `ct-sonic-hi`) and **not one of
them is a voice this agent can speak with.** The config API stored whatever
string arrived, so a save succeeded, the panel said "Saved", and the failure
surfaced on a live call -- which, per decision 29, is not an error but a
*silent mid-call hang*. This module moves that failure from the call to the
save.

Three jobs, in the order they matter:

1. **Reject at save time.** `rejection_reason` is what
   `AgentConfig._check_voice_model` calls. An ID that cannot be synthesised
   never reaches disk.
2. **Serve the real list.** `GET /v1/voices` returns `selectable_voices()` in
   the panel's own `VoiceModel` shape, so the picker can stop shipping
   fixtures. This closes the frontend's `TODO(api)` on that page. Each entry
   now carries `previewUrl`, an ElevenLabs-hosted MP3 the picker's play
   button can point an `<audio>` at -- see `_PREVIEWS`, which has a caveat
   about how long six of those URLs can be trusted.
3. **Resolve for W2.** `resolve_voice(...).provider_voice_id` is the panel ID
   -> ElevenLabs ID mapping the agent will read at call setup.

## The list is measured, not remembered

Every entry below comes from running `scripts/getfreevocies.py` on 2026-07-26,
which does not read `/v1/voices` -- it *synthesises one character through each
voice* and keeps the ones that return audio. That distinction is the whole
lesson of decision 29: the listing endpoint returns every voice in the
workspace, while synthesising a `professional` (library) voice is a separate,
paid entitlement checked by a different service at a different time. Listing
is not permission.

The 2026-07-26 sweep, unchanged from the 2026-07-23 one: **21 pass, 2 return
`402 Payment Required`** -- and the two that fail are Krish and Maya, the only
Indian-accented voices on the account (decisions 29/31).

**Re-run the sweep before trusting this file after a plan change.** These are
entitlements, not constants; an upgrade makes Krish selectable and this file
will not notice on its own.

## Accent, stated plainly

None of the 21 is Indian-accented. The twin claims to be an Indian candidate,
so **every selectable voice here is a live character break** against
CLAUDE.md §4.3 -- decision 31 accepted that trade knowingly, on the grounds
that a wrong accent is a bad turn where no audio is no conversation. British
voices are ordered first because they are the least-wrong stand-in, not
because they are right. Turn-taking, barge-in and latency can be evaluated on
these; accent and Hinglish register cannot.

This lives in `config_store` rather than `shuo/services/` because it is part
of the *config contract* -- the set of values the panel may send -- and the
audio pipeline must not be imported here (see the package docstring). When
Phase 2's `shuo/providers/` lands, the per-provider half of this table is the
natural thing to move behind that interface.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# The only provider this agent can actually synthesise through. Decision 21
# closed the vendor question on ElevenLabs and rejected Cartesia and Sarvam;
# `shuo/services/tts.py` is an ElevenLabs client and nothing else. A catalogue
# entry naming any other provider is therefore documentation of a rejection,
# never something an operator may select.
ELEVENLABS = "ElevenLabs"

_LOCALES = {
    "british": "en-GB",
    "australian": "en-AU",
    "american": "en-US",
    "indian": "en-IN",
}


class Voice(BaseModel):
    """
    One selectable voice, in the shape the panel's picker wants.

    `id`, `name`, `provider`, `description`, `locale` and `badge` are exactly
    the frontend's `VoiceModel` (`components/voice/voice-model-select.tsx`),
    so `GET /v1/voices` can be dropped into `models={...}` with no adapter.
    The remaining fields are extra; the panel's type ignores them until it
    declares them -- `previewUrl` is the one it is about to, for the play
    button, and it is optional on the wire for the reason given on the field.

    Frozen: the catalogue is module-level shared state, and a caller mutating
    an entry would change what every later request sees.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    # The stable public ID. `el-<given name>` -- ours, not ElevenLabs', so a
    # provider swap does not churn every saved config.
    id: str
    name: str
    provider: str
    description: str
    locale: str
    badge: Optional[str] = None

    # The real provider ID, e.g. `JBFqnCBsd6RMkjVDRZzb`. This is the field
    # W2 hands to the TTS service. `None` where we have no working ID at all
    # (a fixture that never named a real voice).
    provider_voice_id: Optional[str] = Field(
        default=None, alias="providerVoiceId"
    )

    # A hosted MP3 of this voice speaking, for the picker's play button. The
    # panel puts this straight in an `<audio src>`; nothing here fetches it.
    # `None` where ElevenLabs has no preview for the ID (every retired
    # fixture, since none of them names a real voice).
    #
    # Not the same thing as `available`. A preview is ElevenLabs' own hosted
    # sample and plays for Krish and Maya too, which this plan cannot
    # synthesise -- so a preview that plays is not evidence a call will.
    preview_url: Optional[str] = Field(default=None, alias="previewUrl")

    # Whether an operator may choose this *today*. False covers three
    # different situations -- wrong provider, blocked by plan, retired
    # fixture -- and `unavailable_reason` is what distinguishes them to the
    # person reading the save row.
    available: bool = True
    unavailable_reason: Optional[str] = Field(
        default=None, alias="unavailableReason"
    )


# =============================================================================
# PREVIEW AUDIO
# =============================================================================
#
# `preview_url` as ElevenLabs reported it on 2026-07-26, keyed by provider ID
# so the catalogue below stays readable -- these strings are up to 300
# characters and inlining them would bury the table.
#
# Read off `voices.get_all()`, the same listing call `getfreevocies.py` makes.
# Listing is free and involves no synthesis, so unlike the availability column
# these values cost nothing to refresh and say nothing about entitlement.
#
# 🔴 **Two URL shapes, and only one of them is safe to hardcode.**
#
# Seventeen are plain `storage.googleapis.com/...mp3` object URLs with no
# expiry. Six -- Laura, Charlie, George, Brian, Daniel, Krish -- are
# `api.us.elevenlabs.io/v1/voices/<id>/previews/audio?payload=<base64>`, and
# that payload decodes to `{voice_source, filename, timestamp}`. The
# timestamp is the current hour, and it came back identical across two
# listings forty minutes apart, so it rotates on a clock rather than per
# request -- which is the worrying reading, not the reassuring one: a value
# that rotates is a value that eventually stops matching. **George carries
# one**, and George is the live default, so it is the first play button
# anyone presses.
#
# The failure is quiet in the panel's direction -- a dead preview is a play
# button that does nothing, not an error -- so re-run the dump when a preview
# stops playing before looking anywhere else. Verified 200 audio/mpeg on
# 2026-07-26. The durable fix, if this bites, is a backend redirect endpoint
# that mints one on demand rather than a constant in a file; see the note in
# `config_api.get_voices`.
_PREVIEWS: dict[str, str] = {
    # ── British ───────────────────────────────────────────────────────
    "JBFqnCBsd6RMkjVDRZzb": (  # George -- minted URL, see above
        "https://api.us.elevenlabs.io/v1/voices/JBFqnCBsd6RMkjVDRZzb"
        "/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5"
        "hbWUiOiJlNjIwNmQxYS0wNzIxLTQ3ODctYWFmYi0wNmE2ZTcwNWNhYzUubXAzIiwidG"
        "ltZXN0YW1wIjoxNzg1MDYzNjAwMDAwMDAwfQ%3D%3D"
    ),
    "onwK4e9ZLuTAKqWW03F9": (  # Daniel -- minted URL
        "https://api.us.elevenlabs.io/v1/voices/onwK4e9ZLuTAKqWW03F9"
        "/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5"
        "hbWUiOiI3ZWVlMDIzNi0xYTcyLTRiODYtYjMwMy01ZGNhZGMwMDdiYTkubXAzIiwidG"
        "ltZXN0YW1wIjoxNzg1MDYzNjAwMDAwMDAwfQ%3D%3D"
    ),
    "Xb7hH8MSUJpSbSDYk0k2": (  # Alice
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/Xb7hH8MSUJpSbSDYk0k2/d10f7534-11f6-41fe-a012-2de1e482d336.mp3"
    ),
    "pFZP5JQG7iQjIQuC4Bku": (  # Lily
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/pFZP5JQG7iQjIQuC4Bku/89b68b35-b3dd-4348-a84a-a3c13a3c2b30.mp3"
    ),
    # ── Australian ────────────────────────────────────────────────────
    "IKne3meq5aSn9XLyUdCD": (  # Charlie -- minted URL
        "https://api.us.elevenlabs.io/v1/voices/IKne3meq5aSn9XLyUdCD"
        "/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5"
        "hbWUiOiIxMDJkZTZmMi0yMmVkLTQzZTAtYTFmMS0xMTFmYTc1YzU0ODEubXAzIiwidG"
        "ltZXN0YW1wIjoxNzg1MDYzNjAwMDAwMDAwfQ%3D%3D"
    ),
    # ── American ──────────────────────────────────────────────────────
    "pNInz6obpgDQGcFmaJgB": (  # Adam
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/pNInz6obpgDQGcFmaJgB/d6905d7a-dd26-4187-bfff-1bd3a5ea7cac.mp3"
    ),
    "nPczCjzI2devNBz1zQrb": (  # Brian -- minted URL
        "https://api.us.elevenlabs.io/v1/voices/nPczCjzI2devNBz1zQrb"
        "/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5"
        "hbWUiOiIyZGQzZTcyYy00ZmQzLTQyZjEtOTNlYS1hYmM1ZDRlNWFhMWQubXAzIiwidG"
        "ltZXN0YW1wIjoxNzg1MDYzNjAwMDAwMDAwfQ%3D%3D"
    ),
    "pqHfZKP75CvOlQylNhV4": (  # Bill
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/pqHfZKP75CvOlQylNhV4/d782b3ff-84ba-4029-848c-acf01285524d.mp3"
    ),
    "cjVigY5qzO86Huf0OWal": (  # Eric
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/cjVigY5qzO86Huf0OWal/d098fda0-6456-4030-b3d8-63aa048c9070.mp3"
    ),
    "iP95p4xoKVk53GoZ742B": (  # Chris
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/iP95p4xoKVk53GoZ742B/3f4bde72-cc48-40dd-829f-57fbf906f4d7.mp3"
    ),
    "CwhRBWXzGAHq8TQ4Fs17": (  # Roger
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/CwhRBWXzGAHq8TQ4Fs17/58ee3ff5-f6f2-4628-93b8-e38eb31806b0.mp3"
    ),
    "bIHbv24MWmeRgasZH58o": (  # Will
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/bIHbv24MWmeRgasZH58o/8caf8f3d-ad29-4980-af41-53f20c72d7a4.mp3"
    ),
    "TX3LPaxmHKxFdv7VOQHJ": (  # Liam
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/TX3LPaxmHKxFdv7VOQHJ/63148076-6363-42db-aea8-31424308b92c.mp3"
    ),
    "N2lVS1w4EtoT3dr4eOWO": (  # Callum
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/N2lVS1w4EtoT3dr4eOWO/ac833bd8-ffda-4938-9ebc-b0f99ca25481.mp3"
    ),
    "SOYHLrjzK2X1ezoPC6cr": (  # Harry
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/SOYHLrjzK2X1ezoPC6cr/86d178f6-f4b6-4e0e-85be-3de19f490794.mp3"
    ),
    "SAz9YHcvj6GT2YYXdXww": (  # River
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/SAz9YHcvj6GT2YYXdXww/e6c95f0b-2227-491a-b3d7-2249240decb7.mp3"
    ),
    "EXAVITQu4vr4xnSDxMaL": (  # Sarah
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/EXAVITQu4vr4xnSDxMaL/01a3e33c-6e99-4ee7-8543-ff2216a32186.mp3"
    ),
    "FGY2WhTYpPnrIDTdsKH5": (  # Laura -- minted URL
        "https://api.us.elevenlabs.io/v1/voices/FGY2WhTYpPnrIDTdsKH5"
        "/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5"
        "hbWUiOiI2NzM0MTc1OS1hZDA4LTQxYTUtYmU2ZS1kZTEyZmU0NDg2MTgubXAzIiwidG"
        "ltZXN0YW1wIjoxNzg1MDYzNjAwMDAwMDAwfQ%3D%3D"
    ),
    "cgSgspJ2msm6clMCkdW9": (  # Jessica
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/cgSgspJ2msm6clMCkdW9/56a97bf8-b69b-448f-846c-c3a11683d45a.mp3"
    ),
    "hpp4J3VqNfWAUOO0d1Us": (  # Bella
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/hpp4J3VqNfWAUOO0d1Us/dab0f5ba-3aa4-48a8-9fad-f138fea1126d.mp3"
    ),
    "XrExE9yKIg1WjnnlVkGX": (  # Matilda
        "https://storage.googleapis.com/eleven-public-prod/premade/voices"
        "/XrExE9yKIg1WjnnlVkGX/b930e18d-6b4d-466e-bab2-0ae97c6d8535.mp3"
    ),
    # ── Blocked by the plan, but the preview still plays ───────────────
    #
    # Deliberately kept. These are the two Indian-accented voices, and an
    # operator arguing for the upgrade (decision 31) wants to hear what the
    # plan is withholding. A preview is a hosted file, not a synthesis call,
    # so it works where a call would go silent.
    "MmiGAbOYCaIFzgNItUWa": (  # Krish -- minted URL
        "https://api.us.elevenlabs.io/v1/voices/MmiGAbOYCaIFzgNItUWa"
        "/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJjdXN0b20iLCJ3b3Jrc3B"
        "hY2VfaWQiOiIxOWJkMjcyZTEzY2Q0YzlkYWI1MzkwMDUyOTFiZTk4NSIsImZpbGVuYW"
        "1lIjoiTEUwY3lpMkhSazlJVjFXb1duV20ubXAzIiwidGltZXN0YW1wIjoxNzg1MDYzN"
        "jAwMDAwMDAwfQ%3D%3D"
    ),
    "4O1sYUnmtThcBoSBrri7": (  # Maya
        "https://storage.googleapis.com/eleven-public-prod/database/workspace"
        "/514d94e9241c48e8b7905375729c436f/voices/4O1sYUnmtThcBoSBrri7"
        "/H62OViSeqwMim7rCIDvv.mp3"
    ),
}


def _voice(
    ident: str,
    name: str,
    provider_voice_id: str,
    accent: str,
    description: str,
    badge: Optional[str] = None,
) -> Voice:
    """A voice confirmed synthesisable by the sweep."""
    return Voice(
        id=ident,
        name=name,
        provider=ELEVENLABS,
        description=description,
        locale=_LOCALES[accent],
        badge=badge,
        provider_voice_id=provider_voice_id,
        preview_url=_PREVIEWS.get(provider_voice_id),
        available=True,
    )


def _unavailable(
    ident: str,
    name: str,
    provider: str,
    reason: str,
    provider_voice_id: Optional[str] = None,
    accent: str = "american",
) -> Voice:
    """
    A voice an operator might plausibly submit but must not be given.

    Carried in the catalogue rather than left to fall through to "unknown ID"
    because the two rejections are not equally useful. "Unknown" tells the
    operator they typed something wrong; `reason` tells them *why the voice
    they deliberately picked is refused*, which for the Krish case is the
    difference between an upgrade and an afternoon of debugging silence.
    """
    return Voice(
        id=ident,
        name=name,
        provider=provider,
        description=reason,
        locale=_LOCALES[accent],
        provider_voice_id=provider_voice_id,
        # `None` for the retired fixtures -- none of them names a real voice,
        # so there is nothing to preview and the panel simply has no play
        # button on a row it should not be rendering anyway.
        preview_url=_PREVIEWS.get(provider_voice_id or ""),
        available=False,
        unavailable_reason=reason,
    )


# =============================================================================
# THE CATALOGUE
# =============================================================================
#
# Descriptions are ours, condensed from the voice labels ElevenLabs returned in
# the sweep. British first, then Australian, then American -- see the accent
# note in the module docstring. Ordering is the picker's ordering.

VOICE_CATALOGUE: tuple[Voice, ...] = (
    # ── British ───────────────────────────────────────────────────────
    _voice(
        "el-george",
        "George",
        "JBFqnCBsd6RMkjVDRZzb",
        "british",
        "Warm and captivating. Currently the live default.",
        badge="Current default",
    ),
    _voice(
        "el-daniel",
        "Daniel",
        "onwK4e9ZLuTAKqWW03F9",
        "british",
        "Steady broadcaster. Even pace, holds up on a compressed line.",
    ),
    _voice(
        "el-alice",
        "Alice",
        "Xb7hH8MSUJpSbSDYk0k2",
        "british",
        "Clear and engaging. The most intelligible of the British set.",
    ),
    _voice(
        "el-lily",
        "Lily",
        "pFZP5JQG7iQjIQuC4Bku",
        "british",
        "Velvety and theatrical. More performance than conversation.",
    ),
    # ── Australian ────────────────────────────────────────────────────
    _voice(
        "el-charlie",
        "Charlie",
        "IKne3meq5aSn9XLyUdCD",
        "australian",
        "Deep and confident.",
    ),
    # ── American ──────────────────────────────────────────────────────
    _voice(
        "el-adam",
        "Adam",
        "pNInz6obpgDQGcFmaJgB",
        "american",
        "Dominant and firm.",
    ),
    _voice(
        "el-brian",
        "Brian",
        "nPczCjzI2devNBz1zQrb",
        "american",
        "Deep and resonant.",
    ),
    _voice(
        "el-bill",
        "Bill",
        "pqHfZKP75CvOlQylNhV4",
        "american",
        "Wise and mature.",
    ),
    _voice(
        "el-eric",
        "Eric",
        "cjVigY5qzO86Huf0OWal",
        "american",
        "Smooth and trustworthy.",
    ),
    _voice(
        "el-chris",
        "Chris",
        "iP95p4xoKVk53GoZ742B",
        "american",
        "Charming and down to earth.",
    ),
    _voice(
        "el-roger",
        "Roger",
        "CwhRBWXzGAHq8TQ4Fs17",
        "american",
        "Laid-back and casual.",
    ),
    _voice(
        "el-will",
        "Will",
        "bIHbv24MWmeRgasZH58o",
        "american",
        "Relaxed optimist.",
    ),
    _voice(
        "el-liam",
        "Liam",
        "TX3LPaxmHKxFdv7VOQHJ",
        "american",
        "Energetic and sociable.",
    ),
    _voice(
        "el-callum",
        "Callum",
        "N2lVS1w4EtoT3dr4eOWO",
        "american",
        "Husky and mischievous.",
    ),
    _voice(
        "el-harry",
        "Harry",
        "SOYHLrjzK2X1ezoPC6cr",
        "american",
        "Fierce and forceful.",
    ),
    _voice(
        "el-river",
        "River",
        "SAz9YHcvj6GT2YYXdXww",
        "american",
        "Relaxed and neutral.",
    ),
    _voice(
        "el-sarah",
        "Sarah",
        "EXAVITQu4vr4xnSDxMaL",
        "american",
        "Mature and reassuring.",
    ),
    _voice(
        "el-laura",
        "Laura",
        "FGY2WhTYpPnrIDTdsKH5",
        "american",
        "Enthusiastic and quirky.",
    ),
    _voice(
        "el-jessica",
        "Jessica",
        "cgSgspJ2msm6clMCkdW9",
        "american",
        "Playful and bright.",
    ),
    _voice(
        "el-bella",
        "Bella",
        "hpp4J3VqNfWAUOO0d1Us",
        "american",
        "Professional and composed.",
    ),
    _voice(
        "el-matilda",
        "Matilda",
        "XrExE9yKIg1WjnnlVkGX",
        "american",
        "Knowledgeable and measured.",
    ),
    # ── Blocked by the plan (decisions 29/31) ─────────────────────────
    #
    # The two voices that would actually suit the persona, and the two the
    # sweep gets 402 on. Kept visible so the refusal names the upgrade
    # instead of reading as a typo.
    _unavailable(
        "el-krish",
        "Krish",
        ELEVENLABS,
        "Indian-accented, and the right voice for this persona — but it is an "
        "ElevenLabs library voice and this plan cannot synthesise it. The API "
        "answers payment_required and the call goes silent rather than "
        "erroring. Upgrade the plan, then re-run scripts/getfreevocies.py",
        provider_voice_id="MmiGAbOYCaIFzgNItUWa",
        accent="indian",
    ),
    _unavailable(
        "el-maya",
        "Maya",
        ELEVENLABS,
        "Indian-accented, and blocked the same way as Krish — this ElevenLabs "
        "plan answers payment_required and the call goes silent. Upgrade the "
        "plan, then re-run scripts/getfreevocies.py",
        provider_voice_id="4O1sYUnmtThcBoSBrri7",
        accent="indian",
    ),
    # ── Retired panel fixtures ────────────────────────────────────────
    #
    # The four IDs the picker shipped before this file existed. They are here
    # for exactly as long as a browser or a saved config might still send
    # one; each says what to pick instead.
    _unavailable(
        "el-rachel-v2",
        "Rachel",
        ELEVENLABS,
        "a placeholder from the old picker. Rachel is not in this ElevenLabs "
        "workspace at all, so it could never have been synthesised. Pick "
        "el-george",
    ),
    _unavailable(
        "el-adam-turbo",
        "Adam Turbo",
        ELEVENLABS,
        "a placeholder from the old picker. The real Adam is el-adam",
    ),
    _unavailable(
        "oa-alloy",
        "Alloy",
        "OpenAI",
        "an OpenAI voice, and this agent only synthesises through ElevenLabs",
    ),
    _unavailable(
        "ct-sonic-hi",
        "Sonic Hindi",
        "Cartesia",
        "a Cartesia voice, and this agent only synthesises through ElevenLabs",
    ),
)


# Two indexes, built once at import. A saved config or a hand-written curl may
# carry either the public ID or the raw provider ID -- an operator who copied
# `JBFqnCBsd6RMkjVDRZzb` out of the ElevenLabs dashboard has not made a
# mistake, and refusing it would be pedantry. Both resolve to the same entry,
# and `_check_voice_model` canonicalises to `Voice.id` before storing so the
# file holds one representation.
_BY_ID = {voice.id.casefold(): voice for voice in VOICE_CATALOGUE}
_BY_PROVIDER_ID = {
    voice.provider_voice_id: voice
    for voice in VOICE_CATALOGUE
    if voice.provider_voice_id
}


def selectable_voices() -> tuple[Voice, ...]:
    """
    The voices an operator may choose, in picker order.

    What `GET /v1/voices` serves. Deliberately excludes the unavailable
    entries: offering a voice the save endpoint will reject is a worse
    experience than not offering it, and the reason still reaches anyone who
    submits one anyway.
    """
    return tuple(voice for voice in VOICE_CATALOGUE if voice.available)


def resolve_voice(value: str) -> Optional[Voice]:
    """
    Look up a voice by public ID or by provider ID. `None` if unknown.

    Returns unavailable entries too -- the caller needs to tell "never heard
    of it" apart from "cannot be synthesised", and only this function knows
    which it is. Use `voice.available` before speaking with it.

    **This is W2's entry point.** At call setup:

        voice = resolve_voice(document.resolved_voice_model)
        if voice is None or not voice.available:
            raise ...                       # loudly, before the call connects
        tts = TTSService(voice_id=voice.provider_voice_id)
    """
    if not value:
        return None
    stripped = value.strip()
    # Provider IDs first, and case-sensitively: ElevenLabs IDs are
    # case-sensitive, and casefolding one before comparing would accept a
    # corrupted ID as valid and then hand the corruption to the synthesiser.
    return _BY_PROVIDER_ID.get(stripped) or _BY_ID.get(stripped.casefold())


def rejection_reason(value: str) -> Optional[str]:
    """
    Why `value` cannot be used, or `None` if it can.

    Returns a clause, not a finished sentence -- the caller supplies the
    subject and the "Nothing was saved." suffix, because this is also used
    from W2 where that suffix would be a lie.
    """
    voice = resolve_voice(value)
    if voice is None:
        count = len(selectable_voices())
        return (
            f"is not a voice this agent can use. Fetch the current list from "
            f"GET /v1/voices ({count} available) and pick one from there"
        )
    if not voice.available:
        return f"({voice.name}) is {voice.unavailable_reason}"
    return None
