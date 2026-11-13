import json
import os
import re
import subprocess
import uuid

try:
    import getmac
except Exception:  # pragma: no cover - optional dependency
    getmac = None


def normalize_mac(raw) -> str:
    """Normalize common MAC formats into a canonical six-byte lowercase string."""
    if raw is None:
        return "00:00:00:00:00:00"

    if isinstance(raw, int):
        value = raw & ((1 << 48) - 1)
        return ":".join(f"{(value >> shift) & 0xFF:02x}" for shift in range(40, -1, -8))

    text = str(raw).strip().lower().replace("-", ":").replace(".", ":")
    text = re.sub(r"[^0-9a-f:]", "", text)
    if not text:
        return "00:00:00:00:00:00"

    cleaned = re.sub(r"[^0-9a-f]", "", text)
    if len(cleaned) < 12:
        cleaned = cleaned.zfill(12)
    elif len(cleaned) > 12:
        cleaned = cleaned[-12:]

    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def _read_mac_from_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            if "mac_address" in payload:
                return payload.get("mac_address")
            if "mac" in payload:
                return payload.get("mac")
    except Exception:
        pass
    return None


# Interfaces to ask about first, most-specific first. en0 is the built-in
# port on every Mac; the rest are ordinary wired/wireless names on other hosts.
_PREFERRED_INTERFACES = ("en0", "eth0", "en1")


def is_locally_administered(mac: str) -> bool:
    """True when bit 0x02 of the first octet is set.

    Those addresses are assigned by software, not burned into a card: Apple's
    internal anpi*/awdl* links, bridges, VM adapters and randomised Wi-Fi all
    carry the flag. They are useless as a machine identity — they differ per
    host layout and can change — so a real card address always wins.
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except Exception:
        return False


def _hardware_mac_by_device() -> dict:
    """{device: burned-in MAC} จาก ``networksetup -listallhardwareports``

    ต้องอ่านจากที่นี่ ไม่ใช่ getmac/ifconfig เพราะสองตัวนั้นคืน "ที่อยู่ปัจจุบัน"
    ของ interface ซึ่งบนแมคที่เปิด Private Wi-Fi Address เป็นค่าที่ระบบสุ่มขึ้นมา
    และหมุนใหม่เป็นระยะ ส่วน networksetup รายงานค่าที่เบิร์นมากับการ์ดจริง

    ยิ่งกว่านั้น getmac ยังมี cache ภายในของตัวเอง เรียก
    ``getmac.get_mac_address(interface="en0")`` ซ้ำ ๆ ในโปรเซสเดียวกันได้คนละค่า
    (รอบแรกได้ของการ์ด รอบต่อไปได้ตัวสุ่ม) จึงเชื่อถือเป็นตัวตนเครื่องไม่ได้เลย
    """
    try:
        out = subprocess.check_output(
            ["networksetup", "-listallhardwareports"],
            text=True, stderr=subprocess.DEVNULL, timeout=10,
        )
    except Exception:
        return {}
    mapping = {}
    device = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Device:"):
            device = line.split(":", 1)[1].strip()
        elif line.startswith("Ethernet Address:") and device:
            mapping[device] = line.split(":", 1)[1].strip()
            device = None
    return mapping


def card_macs() -> list:
    """Every burned-in card MAC on this machine, preferred interfaces first.

    Software-assigned addresses (anpi*, bridges, Thunderbolt) are left out:
    no install ever registered one of them with the admin as a card.
    """
    hardware = _hardware_mac_by_device()
    ordered = [hardware[i] for i in _PREFERRED_INTERFACES if i in hardware]
    ordered += [hardware[i] for i in sorted(hardware) if i not in _PREFERRED_INTERFACES]
    macs = []
    for raw in ordered:
        mac = normalize_mac(raw)
        if mac != "00:00:00:00:00:00" and not is_locally_administered(mac) and mac not in macs:
            macs.append(mac)
    return macs


def _candidate_mac_values():
    env_value = os.environ.get("AICAMERA_MAC_ADDRESS") or os.environ.get("MAC_ADDRESS")
    if env_value:
        yield env_value

    # แหล่งที่นิ่งที่สุดมาก่อน: ที่อยู่ที่เบิร์นมากับการ์ด เรียงตาม interface
    # ที่เราเชื่อถือก่อน แล้วค่อยตัวที่เหลือ (เรียงชื่อเพื่อให้ผลคงเดิมทุกครั้ง)
    hardware = _hardware_mac_by_device()
    for iface in _PREFERRED_INTERFACES:
        if iface in hardware:
            yield hardware[iface]
    for iface in sorted(hardware):
        if iface not in _PREFERRED_INTERFACES:
            yield hardware[iface]

    # Ask the network stack before anything cached or guessed. uuid.getnode()
    # used to come first and it returns whichever interface the OS lists first
    # — on Apple silicon that is anpi0/anpi1, an internal SoC link, so every
    # machine registered a MAC that belongs to no network card at all.
    if getmac is not None:
        for iface in _PREFERRED_INTERFACES:
            try:
                yield getmac.get_mac_address(interface=iface)
            except Exception:
                pass
        try:
            yield getmac.get_mac_address()
        except Exception:
            pass

    app_support_dir = os.environ.get("AICAMERA_APP_SUPPORT_DIR") or os.path.expanduser("~/Library/Application Support/AICamera")
    for json_name in ("device_info.json", "client_config.json"):
        candidate = os.path.join(app_support_dir, json_name)
        if os.path.exists(candidate):
            yield _read_mac_from_json(candidate)

    # Last resort: this can also invent a random address when it cannot find a
    # card, and a random one changes on every run.
    try:
        yield uuid.getnode()
    except Exception:
        pass


def get_canonical_mac_address() -> str:
    """Return the single canonical MAC used across the app and activation flow.

    Two passes: a real card address is taken over a software-assigned one no
    matter what order they came in. That also repairs a machine whose stored
    client_config.json still holds an anpi*/bridge address from an older build
    — the cached value simply loses to the card.
    """
    candidates = []
    for candidate in _candidate_mac_values():
        normalized = normalize_mac(candidate)
        if normalized == "00:00:00:00:00:00" or normalized in candidates:
            continue
        candidates.append(normalized)

    for mac in candidates:
        if not is_locally_administered(mac):
            return mac
    return candidates[0] if candidates else "00:00:00:00:00:00"
