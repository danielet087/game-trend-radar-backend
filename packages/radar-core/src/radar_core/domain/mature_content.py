"""Shared Steam primary-content screening, independent of game identities."""
from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import re

POLICY_VERSION = "steam-primary-content-2026-11-08-v2"
# Migration evidence from the original official descriptor audit. This only
# removes old descriptor-4-only blacklisting; it never grants admission.
LEGACY_DESCRIPTOR4_ONLY = frozenset({
    2532010, 4100630, 4310350, 4790930, 4801860, 5102980, 5189130, 5191820,
})
PRIMARY_EROTIC = re.compile(
    r"\b(?:hentai|pornograph(?:y|ic)|porn\s+(?:game|simulator)|"
    r"(?:sex|sexual)\s+(?:game|simulator|simulation)|"
    r"erotic\s+(?:game|visual novel|simulator|adventure)|adult\s+visual\s+novel|"
    r"explicit\s+sexual\s+game|h[- ]?scenes?|nsfw\s+(?:routes?|scenes?)|"
    r"breast\s+expansion)\b|色情(?:遊戲|游戏)|性愛(?:遊戲|模擬)|性爱(?:游戏|模拟)|H\s*[場场]景",
    re.I,
)
NEGATED = re.compile(r"\b(?:no|not|without|never|doesn.t|unlike)\b[^.;!?\n]{0,55}$", re.I)


@dataclass(frozen=True)
class ContentDecision:
    status: str
    reason: str

    def record(self):
        return {"policy_version": POLICY_VERSION, "status": self.status, "reason": self.reason}


def _text(value):
    return unescape(re.sub(r"<[^>]*>", " ", value[:200000])) if isinstance(value, str) else ""


def _primary(text):
    return any(not NEGATED.search(text[max(0, match.start() - 70):match.start()])
               for match in PRIMARY_EROTIC.finditer(text))


def _descriptors(item):
    raw = item.get("content_descriptorids")
    if raw is None:
        raw = item.get("content_descriptors") or []
    if isinstance(raw, dict):
        raw = raw.get("ids") or []
    try:
        return set(raw)
    except (TypeError, ValueError, OverflowError):
        return set()


def decide_content(item, details=None, *, hard_ids=frozenset({3})):
    """Use primary marketing context, never age-rating notes or AppID exemptions.

    Descriptor 3 is decisive. Descriptor 4 triggers a bounded official detail
    lookup by the caller. Missing/ambiguous context is retryable review, not a
    permanent adult-game exclusion. Rating notes about sex/nudity are evidence
    of mature scenes, not proof that the game's primary purpose is erotic.
    """
    if not isinstance(item, dict):
        return ContentDecision("review", "invalid_content_metadata")
    details = details if isinstance(details, dict) else item.get("content_details")
    details = details if isinstance(details, dict) else None
    ids = _descriptors(item)
    if ids & (set(hard_ids) - {4}):
        return ContentDecision("excluded", "adult_only_descriptor")
    appid = item.get("appid", item.get("steam_appid"))
    if details is not None:
        detail_ids = _descriptors(details)
        mismatched = (details.get("steam_appid") is not None and appid is not None
            and (type(details["steam_appid"]) is not int or details["steam_appid"] != appid))
        if mismatched:
            if (ids | detail_ids) & {3, 4}:
                return ContentDecision("review", "official_content_identity_mismatch")
            details = None  # Unrelated optional data cannot change an ordinary row.
        else:
            ids |= detail_ids
            if ids & (set(hard_ids) - {4}):
                return ContentDecision("excluded", "adult_only_descriptor")
    basic = item.get("basic_info") or {}
    basic = basic if isinstance(basic, dict) else {}
    short = "\n".join(_text(value) for value in (
        item.get("name"), basic.get("short_description"), item.get("short_description")))
    if _primary(short):
        return ContentDecision("excluded", "primary_erotic_marketing")
    if 4 not in ids:
        return ContentDecision("screened", "no_primary_erotic_evidence")
    appid = item.get("appid", item.get("steam_appid"))
    if (details is None or type(appid) is not int
            or type(details.get("steam_appid")) is not int or details["steam_appid"] != appid):
        return ContentDecision("review", "official_content_context_unavailable")
    marketing = "\n".join(_text(details.get(field)) for field in (
        "short_description", "about_the_game", "detailed_description"))
    if not marketing.strip():
        return ContentDecision("review", "official_content_context_empty")
    if _primary(marketing):
        return ContentDecision("excluded", "primary_erotic_marketing")
    tags = [row.get("tagid") for row in item.get("tags") or [] if isinstance(row, dict)]
    if 9130 in tags[:5] or 65443 in tags[:5]:
        return ContentDecision("review", "ambiguous_primary_erotic_context")
    return ContentDecision("screened", "ordinary_mature_game")


def reviewed_descriptors(appid, descriptors: set) -> set:
    """Compatibility: descriptor 4 alone is never an automatic porn verdict."""
    return descriptors - {4}
