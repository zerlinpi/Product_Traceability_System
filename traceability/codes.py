from __future__ import annotations

import re
import secrets
from io import BytesIO

import qrcode
import qrcode.image.svg
from qrcode.constants import ERROR_CORRECT_M


SN_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{3,63}$")
ENTITY_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")
STATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def normalize_sn(value: object) -> str:
    sn = str(value or "").strip().upper()
    if not SN_PATTERN.fullmatch(sn):
        raise ValueError("SN 需为 4-64 位大写字母、数字、下划线或短横线")
    return sn


def normalize_entity_code(value: object, label: str) -> str:
    code = str(value or "").strip().upper()
    if not ENTITY_CODE_PATTERN.fullmatch(code):
        raise ValueError(f"{label}需为 2-32 位字母、数字、下划线或短横线")
    return code


def normalize_station_id(value: object) -> str:
    station_id = str(value or "").strip()
    if not STATION_ID_PATTERN.fullmatch(station_id):
        raise ValueError("工位标识无效，请刷新页面后重试")
    return station_id


def machine_identification_code(sn: str) -> str:
    return f"PTS:M:{sn}"


def new_part_identification_code() -> str:
    return f"PTS:P:{secrets.token_hex(12).upper()}"


def new_batch_code(now_compact: str, prefix: str) -> str:
    return f"B-{prefix}-{now_compact}-{secrets.token_hex(3).upper()}"


def batch_identification_code(batch_code: str) -> str:
    return f"PTS:B:{batch_code}"


def parse_batch_payload(payload: object) -> str:
    code = str(payload or "").strip()
    if not code:
        raise ValueError("批次码无效")
    if code.startswith("PTS:B:"):
        code = code[len("PTS:B:"):].strip()
    if not code:
        raise ValueError("批次码无效")
    return code


def new_label_batch_code(now_compact: str) -> str:
    return f"PB-{now_compact}-{secrets.token_hex(3).upper()}"


def new_trace_number(now_compact: str) -> str:
    return f"TR-{now_compact}-{secrets.token_hex(3).upper()}"


def new_event_id() -> str:
    return f"EV-{secrets.token_hex(12).upper()}"


def make_qr_svg(payload: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=10,
        border=3,
        image_factory=qrcode.image.svg.SvgPathImage,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    output = BytesIO()
    qr.make_image(attrib={"class": "trace-qr"}).save(output)
    return output.getvalue()
