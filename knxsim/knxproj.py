"""Import devices from an ETS project file (``.knxproj``).

A ``.knxproj`` is a ZIP archive. ETS 5/6 keep the project in ``P-XXXX/0.xml``
(topology, group addresses, device instances) and the product data of every
device used in ``M-XXXX/*.xml`` (hardware, application programs, com objects).

The result is a list of devices in KNXSim's configuration format; every group
object linked to a group address becomes a generic ``object`` channel:

    {"ia": "1.1.2", "name": "Dimming actuator",
     "manufacturer_id": 131, "application_id": 160, "order_number": "5WG1...",
     "mask_version": 1792,
     "channels": [{"type": "object", "number": 1, "name": "Switch",
                   "dpt": "1.001", "flags": "rwt", "ga": {"value": ["1/2/1"]}}]}

DPT and flags come from the device's com-object instance, then the product's
ComObjectRef and ComObject, then the linked group address, then the object size.
"""
from __future__ import annotations

import base64
import binascii
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .protocol import ga_to_str

_NAMESPACE = re.compile(r"^\{[^}]*\}")
_FLAGS = (("ReadFlag", "r"), ("WriteFlag", "w"), ("UpdateFlag", "u"), ("TransmitFlag", "t"))
_SIZE_DPT = {"1 Bit": "1.001", "2 Bit": "2.001", "4 Bit": "3.007", "1 Byte": "5.010",
             "2 Bytes": "7.001", "4 Bytes": "12.001"}


def _tag(elem: ET.Element) -> str:
    return _NAMESPACE.sub("", elem.tag)


def _iter(root: ET.Element, name: str):
    return (elem for elem in root.iter() if _tag(elem) == name)


def _short(ident: str) -> str:
    """``P-0501-0_GA-17`` (ETS 5) and ``GA-17`` (ETS 6) name the same group address."""
    return ident.rsplit("_", 1)[-1]


def _pick(name: str, *sources: dict) -> str | None:
    return next((src[name] for src in sources if src.get(name)), None)


def normalize_dpt(text: str | None) -> str | None:
    """``DPST-1-1`` / ``DPT-9`` -> ``1.001`` / ``9.000``; lists keep the first entry."""
    if not text or not text.split():
        return None
    match = re.fullmatch(r"DPS?T-(\d+)(?:-(\d+))?", text.split()[0], re.IGNORECASE)
    return f"{int(match.group(1))}.{int(match.group(2) or 0):03d}" if match else None


def _parse_serial(text: str | None) -> bytes | None:
    """ETS stores serials as base64 (``AIMBpd8q``); also accept hex ``0083:01A5DF2A``."""
    if not text:
        return None
    value = text.strip().replace(":", "")
    try:
        raw = bytes.fromhex(value) if len(value) == 12 else base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None
    return raw if len(raw) == 6 else None


def _parse_mask(text: str | None) -> int | None:
    try:
        return int(text.strip().upper().removeprefix("MV-"), 16) if text else None
    except ValueError:
        return None


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text else None
    except ValueError:
        return None


class _Catalog:
    """Everything the device instances refer to, collected from all XML files."""

    def __init__(self, roots: list[ET.Element]):
        self.addresses: dict[str, tuple[str, str | None]] = {}  # GA-n -> (x/y/z, dpt)
        self.objects: dict[str, dict] = {}       # ComObject Id -> attributes
        self.object_refs: dict[str, dict] = {}   # ComObjectRef Id -> attributes
        self.programs: dict[str, dict] = {}      # ApplicationProgram Id -> identity
        self.hw2prog: dict[str, str] = {}        # Hardware2Program Id -> ApplicationProgram Id
        self.products: dict[str, dict] = {}      # Product Id -> attributes
        for root in roots:
            for elem in _iter(root, "GroupAddress"):
                if elem.get("Id") and elem.get("Address"):
                    address = elem.get("Address")
                    if address.isdigit():
                        address = ga_to_str(int(address))
                    self.addresses[_short(elem.get("Id"))] = (address, normalize_dpt(elem.get("DatapointType")))
            for elem in _iter(root, "ComObject"):
                self.objects[elem.get("Id", "")] = elem.attrib
            for elem in _iter(root, "ComObjectRef"):
                self.object_refs[elem.get("Id", "")] = elem.attrib
            for elem in _iter(root, "ApplicationProgram"):
                self.programs[elem.get("Id", "")] = {
                    "application_id": _int(elem.get("ApplicationNumber")),
                    "application_version": _int(elem.get("ApplicationVersion")),
                    "mask_version": _parse_mask(elem.get("MaskVersion")),
                }
            for elem in _iter(root, "Hardware2Program"):
                ref = next(_iter(elem, "ApplicationProgramRef"), None)
                if ref is not None:
                    self.hw2prog[elem.get("Id", "")] = ref.get("RefId", "")
            for elem in _iter(root, "Product"):
                self.products[elem.get("Id", "")] = elem.attrib

    def identity(self, hardware_ref: str) -> tuple[str, dict]:
        """Application program id and manufacturer/application identity of a device."""
        program_id = self.hw2prog.get(hardware_ref) or re.sub(r"_H-.*?_HP-", "_A-", hardware_ref)
        identity = dict(self.programs.get(program_id) or {})
        if identity.get("application_id") is None:  # product data missing: decode the id itself
            match = re.search(r"_(?:A|HP)-([0-9A-Fa-f]{4})-([0-9A-Fa-f]{2})", hardware_ref)
            if match:
                identity.update(application_id=int(match.group(1), 16),
                                application_version=int(match.group(2), 16))
        match = re.match(r"M-([0-9A-Fa-f]{4})", hardware_ref)
        if match:
            identity["manufacturer_id"] = int(match.group(1), 16)
        return program_id, {k: v for k, v in identity.items() if v is not None}

    def channel(self, inst: ET.Element, program_id: str) -> dict | None:
        links = (inst.get("Links") or "").split()  # ETS 5.7+ / 6
        links += [c.get("GroupAddressRefId", "") for c in inst.iter() if _tag(c) in ("Send", "Receive")]  # ETS 4
        linked = [self.addresses[_short(link)] for link in links if _short(link) in self.addresses]
        if not linked:
            return None  # unlinked object: nothing to simulate on the bus
        ref_id = inst.get("RefId", "")
        if not ref_id.startswith("M-"):  # ETS 6 writes "O-1_R-1" relative to the application
            ref_id = f"{program_id}_{ref_id}"
        ref = self.object_refs.get(ref_id, {})
        obj = self.objects.get(ref.get("RefId") or re.sub(r"_R-\d+$", "", ref_id), {})
        own = inst.attrib
        number = _int(obj.get("Number"))
        if number is None:
            match = re.search(r"O-(\d+)", ref_id)
            number = int(match.group(1)) if match else 0
        dpt = (normalize_dpt(_pick("DatapointType", own, ref, obj)) or linked[0][1]
               or _SIZE_DPT.get(_pick("ObjectSize", own, ref, obj) or "") or "1.001")
        flags = "".join(letter for attr, letter in _FLAGS
                        if (_pick(attr, own, ref, obj) or "").lower() in ("enabled", "true", "1"))
        text = _pick("Text", own, ref, obj) or obj.get("Name") or f"Object {number}"
        function = _pick("FunctionText", own, ref, obj)
        return {
            "type": "object",
            "number": number,
            "name": f"{text} - {function}" if function and function != text else text,
            "dpt": dpt,
            "flags": flags or "rw",  # no product data: accept writes, answer reads
            "ga": {"value": [address for address, _ in linked]},
        }

    def device(self, elem: ET.Element, ia: str) -> dict:
        program_id, identity = self.identity(elem.get("Hardware2ProgramRefId", ""))
        product = self.products.get(elem.get("ProductRefId", ""), {})
        channels = [ch for ch in (self.channel(inst, program_id) for inst in _iter(elem, "ComObjectInstanceRef")) if ch]
        cfg = {
            "ia": ia,
            "name": elem.get("Name") or product.get("Text") or f"Device {ia}",
            "load_state": 0,  # nothing downloaded into this simulator yet
            **identity,
            "channels": channels,
        }
        serial = _parse_serial(elem.get("SerialNumber"))
        if serial:
            cfg["serial"] = serial.hex()
        if product.get("OrderNumber"):
            cfg["order_number"] = product["OrderNumber"]
        return cfg


def _read_roots(path: Path) -> list[ET.Element]:
    # ponytail: parses every product XML fully; switch to iterparse if huge application files hurt
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if any(re.fullmatch(r"P-[0-9A-Fa-f]+\.zip", name) for name in names):
                raise ValueError(f"{path}: the project is password-protected; "
                                 "export it from ETS without a password")
            roots = []
            for name in names:
                if name.lower().endswith(".xml") and not name.lower().endswith("knx_master.xml"):
                    try:
                        roots.append(ET.fromstring(archive.read(name)))
                    except ET.ParseError:
                        continue
            return roots
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"{path}: not a readable ETS project ({exc})") from exc


def load_project(path: str | Path) -> dict:
    """Load an ETS ``.knxproj`` and return ``{"name", "devices"}``.

    Devices are returned in KNXSim's configuration format; every instance that
    uses them builds its own Device objects (separate memory and state).
    """
    path = Path(path)
    roots = _read_roots(path)
    catalog = _Catalog(roots)
    devices = []
    for root in roots:
        for area in _iter(root, "Area"):
            for line in (child for child in area if _tag(child) == "Line"):
                for elem in _iter(line, "DeviceInstance"):  # ETS 6 nests them in <Segment>
                    if elem.get("Address") is not None:  # no address: not placed in the line yet
                        ia = f"{area.get('Address')}.{line.get('Address')}.{elem.get('Address')}"
                        devices.append(catalog.device(elem, ia))
    if not devices:
        raise ValueError(f"{path}: no device instances with an address found (is this a .knxproj?)")
    name = next((e.get("Name") for root in roots for e in _iter(root, "ProjectInformation") if e.get("Name")),
                path.stem)
    return {"name": name, "devices": devices}
