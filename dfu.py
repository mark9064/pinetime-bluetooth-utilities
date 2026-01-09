import functools
import sys
import json
import zipfile
import time
import math

import anyio
import bleak

DEVICE_NAME = "InfiniTime"
SEGMENT_REPORT_INTERVAL = 50
DATA_RATE = 200 * 1024 / 8  # 200kb/s
CHUNK_SIZE = 200 # technically a spec violation, but works as the buffer on device is 200 bytes

CONTROL_POINT = "00001531-1212-efde-1523-785feabcd123"
PACKET = "00001532-1212-efde-1523-785feabcd123"


def control_point_response(
    queue: anyio.streams.memory.MemoryObjectSendStream,
    _: bleak.BleakGATTCharacteristic,
    data: bytearray,
) -> None:
    queue.send_nowait(data)


def load_firmware(path: str) -> tuple[bytes, bytes]:
    with zipfile.ZipFile(path) as file:
        manifest = json.loads(file.read("manifest.json"))
        bin_file = file.read(manifest["manifest"]["application"]["bin_file"])
        dat_file = file.read(manifest["manifest"]["application"]["dat_file"])
    return (bin_file, dat_file)


async def main() -> None:
    if len(sys.argv) == 1 or sys.argv[1] in {"-h", "--help"}:
        print("Usage: python dfu.py </path/to/pinetime-mcuboot-app-dfu.zip>")
        return
    firmware_bin, firmware_dat = load_firmware(sys.argv[1])
    firmware_size = len(firmware_bin)
    print("Searching")
    device = await bleak.BleakScanner.find_device_by_name(DEVICE_NAME)
    if device is None:
        print("Can't find an InfiniTime device. Disconnect the watch from any device")
        return
    client = bleak.BleakClient(device)
    print("Connecting")
    await client.connect(timeout=15)
    print("Connected")
    if (
        client.services.get_characteristic(PACKET).max_write_without_response_size
        < CHUNK_SIZE
    ):
        print(
            "Reported MTU too small! However the bluetooth stack is usually incorrect, continuing anyway..."
        )
    receive_stream: anyio.streams.memory.MemoryObjectReceiveStream
    send_stream, receive_stream = anyio.create_memory_object_stream[bytearray](
        max_buffer_size=math.inf
    )
    await client.start_notify(
        CONTROL_POINT, functools.partial(control_point_response, send_stream)
    )
    await client.write_gatt_char(CONTROL_POINT, bytes([0x01, 0x04]), response=False)
    await client.write_gatt_char(
        PACKET, bytes(8) + firmware_size.to_bytes(4, "little"), response=False
    )
    try:
        with anyio.fail_after(10):
            if (await receive_stream.receive()) != bytes([0x10, 0x01, 0x01]):
                raise RuntimeError("DFU start failed")
    except TimeoutError:
        print("DFU start failed: timeout. Check DFU is enabled on the Over-the-air settings page")
        await client.disconnect()
        return
    await client.write_gatt_char(CONTROL_POINT, bytes([0x02, 0x00]), response=False)
    await client.write_gatt_char(PACKET, firmware_dat, response=False)
    await client.write_gatt_char(CONTROL_POINT, bytes([0x02, 0x01]), response=False)
    if (await receive_stream.receive()) != bytes([0x10, 0x02, 0x01]):
        raise RuntimeError("DAT file send failed")
    await client.write_gatt_char(
        CONTROL_POINT, bytes([0x08, SEGMENT_REPORT_INTERVAL]), response=False
    )
    await client.write_gatt_char(CONTROL_POINT, bytes([0x03]), response=False)
    sent_size = 0
    sent_chunks = 0
    start = time.monotonic()
    while sent_size < firmware_size:
        next_chunk = firmware_bin[sent_size : sent_size + CHUNK_SIZE]
        sent_size += len(next_chunk)
        sent_chunks += 1
        await client.write_gatt_char(PACKET, next_chunk, response=False)
        print(f"{sent_chunks} chunks | {sent_size}B")
        try:
            status = receive_stream.receive_nowait()
        except anyio.WouldBlock:
            pass
        else:
            if status[0] != 0x11:
                raise RuntimeError("Bad transfer status")
            print(
                "ACK: {0}B".format(int.from_bytes(status[1:], "little", signed=False))
            )
        sent_bytes_target = (time.monotonic() - start) * DATA_RATE
        if sent_size > sent_bytes_target:
            await anyio.sleep((sent_size - sent_bytes_target) / DATA_RATE)
    while (status := await receive_stream.receive())[0] == 0x11:
        print(
            "ACK (queued data): {0}B".format(
                int.from_bytes(status[1:], "little", signed=False)
            )
        )
    if status != bytes([0x10, 0x03, 0x01]):
        raise RuntimeError("Firmware receive failed")
    print("Validating firmware integrity")
    await client.write_gatt_char(CONTROL_POINT, bytes([0x04]), response=False)
    if (await receive_stream.receive()) != bytes([0x10, 0x04, 0x01]):
        raise RuntimeError("Firmware validation failed")
    await client.write_gatt_char(CONTROL_POINT, bytes([0x05]), response=False)
    await client.disconnect()
    print("DFU complete!")


if __name__ == "__main__":
    anyio.run(main)
