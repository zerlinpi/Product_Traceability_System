from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import re
import threading
from dataclasses import asdict, dataclass


V2_NAME_PREFIX = "TW-04"
V2_NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
SN_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{3,63}$")

# A Bluetooth adapter can only run one discovery/read at a time. Two concurrent
# requests would fight over the radio and produce either a spurious failure or a
# device attributed to the wrong workstation, so the endpoints take this
# non-blockingly and answer 409 instead of queueing — a scan gun operator should
# be told to retry, not left waiting behind an unrelated read.
#
# It lives here rather than in app.py because it guards the radio, not the HTTP
# layer: anything that talks to a device must take it.
BLUETOOTH_OPERATION_LOCK = threading.Lock()


class BluetoothCollectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class BluetoothDeviceInfo:
    name: str
    address: str


@dataclass(frozen=True)
class ManufacturerIdentity:
    name: str
    address: str
    sn: str
    model: str
    production_date: str


def is_bluetooth_runtime_available() -> bool:
    return importlib.util.find_spec("bleak") is not None


def _require_bleak():
    if not is_bluetooth_runtime_available():
        raise BluetoothCollectionError("蓝牙采集组件未安装，请先运行 install.bat")
    from bleak import BleakClient, BleakScanner

    return BleakClient, BleakScanner


async def _discover(timeout: float, name_prefixes: tuple[str, ...]) -> list[BluetoothDeviceInfo]:
    _client, scanner = _require_bleak()
    try:
        devices = await scanner.discover(timeout=timeout)
    except Exception as error:
        raise BluetoothCollectionError(f"蓝牙扫描失败：{error}") from error
    matched: dict[str, BluetoothDeviceInfo] = {}
    for device in devices:
        name = str(getattr(device, "name", "") or "").strip()
        address = str(getattr(device, "address", "") or "").strip()
        if any(name.startswith(prefix) for prefix in name_prefixes) and address:
            matched[address] = BluetoothDeviceInfo(name=name, address=address)
    return sorted(matched.values(), key=lambda item: (item.name, item.address))


def discover_v2_devices(
    timeout: float = 6.0,
    name_prefixes: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    timeout = min(15.0, max(2.0, float(timeout)))
    prefixes = tuple(
        prefix.strip() for prefix in (name_prefixes or [V2_NAME_PREFIX]) if prefix.strip()
    )
    if not prefixes:
        raise BluetoothCollectionError("未配置可扫描的蓝牙产品型号")
    return [asdict(item) for item in asyncio.run(_discover(timeout, prefixes))]


def _extract_identity(message: object) -> tuple[str, str, str] | None:
    if not isinstance(message, dict) or message.get("type") != "telemetry":
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    manufacturer = data.get("manufacturerData")
    if not isinstance(manufacturer, dict):
        return None
    sn = str(manufacturer.get("sn") or "").strip().upper()
    model = str(manufacturer.get("model") or "").strip()
    production_date = str(manufacturer.get("productionDate") or "").strip()
    if not SN_PATTERN.fullmatch(sn):
        return None
    if production_date and not re.fullmatch(r"\d{8}", production_date):
        production_date = ""
    return sn, model, production_date


async def _read_identity(
    address: str,
    name: str,
    timeout: float,
    notify_uuid: str,
) -> ManufacturerIdentity:
    client_class, _scanner = _require_bleak()
    loop = asyncio.get_running_loop()
    identity_future: asyncio.Future[tuple[str, str, str]] = loop.create_future()
    receive_buffer = ""

    def on_notification(_sender, payload: bytearray) -> None:
        nonlocal receive_buffer
        receive_buffer += bytes(payload).decode("utf-8", errors="ignore")
        while "\n" in receive_buffer:
            raw_line, receive_buffer = receive_buffer.split("\n", 1)
            raw_line = raw_line.strip("\r \t")
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            identity = _extract_identity(parsed)
            if identity and not identity_future.done():
                identity_future.set_result(identity)

    try:
        async with client_class(address, timeout=timeout) as client:
            await client.start_notify(notify_uuid, on_notification)
            try:
                sn, model, production_date = await asyncio.wait_for(identity_future, timeout=timeout)
            except TimeoutError as error:
                raise BluetoothCollectionError(
                    "已连接设备，但未从该产品型号配置的通知特征收到完整 SN"
                ) from error
            finally:
                with contextlib.suppress(Exception):
                    await client.stop_notify(notify_uuid)
    except BluetoothCollectionError:
        raise
    except Exception as error:
        raise BluetoothCollectionError(f"读取设备 SN 失败：{error}") from error
    return ManufacturerIdentity(
        name=name or address,
        address=address,
        sn=sn,
        model=model,
        production_date=production_date,
    )


def read_v2_identity(
    address: str,
    name: str = "",
    timeout: float = 10.0,
    notify_uuid: str = V2_NOTIFY_UUID,
) -> dict[str, str]:
    address = str(address or "").strip()
    if not address or len(address) > 120:
        raise BluetoothCollectionError("蓝牙设备地址无效")
    timeout = min(20.0, max(4.0, float(timeout)))
    notify_uuid = str(notify_uuid or "").strip().lower()
    if not notify_uuid:
        raise BluetoothCollectionError("产品型号未配置蓝牙通知 UUID")
    return asdict(
        asyncio.run(_read_identity(address, str(name or "").strip(), timeout, notify_uuid))
    )
