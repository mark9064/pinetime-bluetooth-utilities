import os
import struct
import dataclasses
import datetime
from typing import Optional

import anyio
import bleak

FILESYSTEM = "adaf0200-4669-6c65-5472-616e73666572"


class FSError(Exception):
    error_codes = {
        -5: "Error during device operation",
        -84: "Corrupted",
        -2: "No directory entry",
        -17: "Entry already exists",
        -20: "Entry is not a dir",
        -21: "Entry is a dir",
        -39: "Dir is not empty",
        -9: "Bad file number",
        -27: "File too large",
        -22: "Invalid parameter",
        -28: "No space left on device",
        -12: "No more memory available",
        -61: "No data/attr available",
        -36: "File name too long",
    }

    def __init__(self, ecode: int):
        message = self.error_codes.get(ecode, "Unknown error")
        super().__init__(message)
        self.ecode = ecode


class ProtocolError(Exception):
    pass


@dataclasses.dataclass
class Node:
    name: str
    full_path: str
    modification_time: datetime.datetime


@dataclasses.dataclass
class File(Node):
    size: int


@dataclasses.dataclass
class Directory(Node):
    pass


class CommandBuilder:
    @staticmethod
    def read(path: str, maxsize: int) -> bytes:
        return struct.pack("<BxHII", 0x10, len(path), 0, maxsize) + path.encode("ascii")

    @staticmethod
    def read_data(offset: int, maxsize: int) -> bytes:
        return struct.pack("<BbxxII", 0x12, 1, offset, maxsize)

    @staticmethod
    def write(path: str, mtime: datetime.datetime, size: int) -> bytes:
        mtime = int(mtime.timestamp() * 10**9)
        return struct.pack("<BxHIQI", 0x20, len(path), 0, mtime, size) + path.encode(
            "ascii"
        )

    @staticmethod
    def write_data(offset: int, data: bytes) -> bytes:
        return struct.pack("<BbxxII", 0x22, 1, offset, len(data)) + data

    @staticmethod
    def delete(path: str) -> bytes:
        return struct.pack("<BxH", 0x30, len(path)) + path.encode("ascii")

    @staticmethod
    def make_directory(path: str, mtime: datetime.datetime) -> bytes:
        mtime = int(mtime.timestamp() * 10**9)
        return struct.pack("<BxHxxxxQ", 0x40, len(path), mtime) + path.encode("ascii")

    @staticmethod
    def list_directory(path: str) -> bytes:
        return struct.pack("<BxH", 0x50, len(path)) + path.encode("ascii")

    @staticmethod
    def move_node(old_path: str, new_path: str) -> bytes:
        return (
            struct.pack("<BxHH", 0x60, len(old_path), len(new_path))
            + old_path.encode("ascii")
            + bytes(1)
            + new_path.encode("ascii")
        )


class CommandParser:
    @classmethod
    def validate_status(cls, status: int) -> None:
        if status != 1:
            raise FSError(status)

    @classmethod
    def validate_command(cls, expected_command: int, actual_command: int) -> None:
        if expected_command != actual_command:
            raise ProtocolError("Unexpected response command")

    @dataclasses.dataclass
    class Read:
        offset: int
        total_size: int
        data: bytes

    @classmethod
    def read(cls, response: bytes) -> "CommandParser.Read":
        fmtstring = "<BbxxIII"
        command, status, offset, total_size, chunk_size = struct.unpack(
            fmtstring, response[: struct.calcsize(fmtstring)]
        )
        cls.validate_command(0x11, command)
        cls.validate_status(status)
        data = response[struct.calcsize(fmtstring) :]
        if chunk_size != len(data):
            raise ProtocolError("Data size inconsistent")
        return cls.Read(offset=offset, total_size=total_size, data=data)

    @dataclasses.dataclass
    class Write:
        offset: int
        mtime: datetime.datetime
        remaining_space: int

    @classmethod
    def write(cls, response: bytes) -> "CommandParser.Write":
        command, status, offset, mtime, remaining = struct.unpack("<BbxxIQI", response)
        cls.validate_command(0x21, command)
        cls.validate_status(status)
        mtime = datetime.datetime.fromtimestamp(mtime / 10**9)
        return cls.Write(offset=offset, mtime=mtime, remaining_space=remaining)

    @classmethod
    def delete(cls, response: bytes) -> None:
        command, status = struct.unpack("<Bb", response)
        cls.validate_command(0x31, command)
        cls.validate_status(status)

    @dataclasses.dataclass
    class MakeDirectory:
        mtime: datetime.datetime

    @classmethod
    def make_directory(cls, response: bytes) -> "CommandParser.MakeDirectory":
        command, status, mtime = struct.unpack("<BbxxxxxxQ", response)
        cls.validate_command(0x41, command)
        cls.validate_status(status)
        mtime = datetime.datetime.fromtimestamp(mtime / 10**9)
        return cls.MakeDirectory(mtime=mtime)

    @dataclasses.dataclass
    class DirectoryEntry:
        index: int
        total_entries: int
        is_directory: bool
        mtime: datetime.datetime
        size: int
        path: str

    @classmethod
    def list_directory(cls, response: bytes) -> "CommandParser.DirectoryEntry":
        fmtstring = "<BbHIIIQI"
        (
            command,
            status,
            path_length,
            index,
            total_entries,
            flags,
            mtime,
            size,
        ) = struct.unpack(fmtstring, response[: struct.calcsize(fmtstring)])
        cls.validate_command(0x51, command)
        cls.validate_status(status)
        mtime = datetime.datetime.fromtimestamp(mtime / 10**9)
        path = response[struct.calcsize(fmtstring) :]
        if path_length != len(path):
            raise ProtocolError("Path length inconsistent")
        is_directory = flags & 0x1
        return cls.DirectoryEntry(
            index=index,
            total_entries=total_entries,
            is_directory=is_directory,
            mtime=mtime,
            size=size,
            path=path.decode("ascii"),
        )

    @classmethod
    def move_node(cls, response: bytes) -> None:
        command, status = struct.unpack("<Bb", response)
        cls.validate_command(0x61, command)
        cls.validate_status(status)


async def get_notification(
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
) -> bytearray:
    try:
        with anyio.fail_after(5):
            return await notifications.receive()
    except TimeoutError:
        raise ProtocolError("Response timeout") from None


async def read_file(
    client: bleak.BleakClient,
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
    path: str,
) -> bytes:
    await client.write_gatt_char(
        FILESYSTEM, CommandBuilder.read(path, 200), response=False
    )
    received = bytearray()
    while True:
        response = CommandParser.read(await get_notification(notifications))
        if response.offset != len(received):
            raise ProtocolError("Unknown protocol error")
        received.extend(response.data)
        print(f"Received {len(received)}B of {response.total_size}B")  # TODO remove
        if len(received) == response.total_size:
            break
        await client.write_gatt_char(
            FILESYSTEM, CommandBuilder.read_data(len(received), 200), response=False
        )
    return received


async def write_file(
    client: bleak.BleakClient,
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
    path: str,
    data: bytes,
    mtime: Optional[datetime.datetime] = None,
) -> None:
    if mtime is None:
        mtime = datetime.datetime.now()
    size = len(data)
    await client.write_gatt_char(FILESYSTEM, CommandBuilder.write(path, mtime, size))
    offset = 0
    next_chunk = b""
    while True:
        response = CommandParser.write(await get_notification(notifications))
        if response.remaining_space < size - offset:
            raise ProtocolError("Out of space")
        if response.offset != offset:
            raise ProtocolError("Message lost")
        if offset == size:
            break
        offset += len(next_chunk)
        if offset == size:
            next_chunk = b""
        else:
            next_chunk = data[offset : offset + 200]
        await client.write_gatt_char(
            FILESYSTEM, CommandBuilder.write_data(offset, next_chunk)
        )


async def remove_node(
    client: bleak.BleakClient,
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
    path: str,
) -> None:
    await client.write_gatt_char(FILESYSTEM, CommandBuilder.delete(path))
    CommandParser.delete(await get_notification(notifications))


async def make_directory(
    client: bleak.BleakClient,
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
    path: str,
    mtime: Optional[datetime.datetime] = None,
) -> None:
    if mtime is None:
        mtime = datetime.datetime.now()
    await client.write_gatt_char(FILESYSTEM, CommandBuilder.make_directory(path, mtime))
    CommandParser.make_directory(await get_notification(notifications))


async def list_directory(
    client: bleak.BleakClient,
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
    path: str,
) -> dict[str, Node]:
    def retype_response(response: CommandParser.DirectoryEntry) -> Node:
        args = {
            "name": response.path,
            "full_path": os.path.join(path, response.path),
            "modification_time": response.mtime,
        }
        if response.is_directory:
            return Directory(**args)
        return File(**args, size=response.size)

    await client.write_gatt_char(FILESYSTEM, CommandBuilder.list_directory(path))
    response = CommandParser.list_directory(await get_notification(notifications))
    entries = [response]
    last_index = -1
    while response.index < response.total_entries:
        if response.index - 1 != last_index:
            raise ProtocolError("Message lost")
        last_index = response.index
        response = CommandParser.list_directory(await get_notification(notifications))
        if response.index != response.total_entries:
            entries.append(response)
    return {node.name: node for node in [retype_response(resp) for resp in entries]}


async def move(
    client: bleak.BleakClient,
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
    old_path: str,
    new_path: str,
) -> None:
    await client.write_gatt_char(
        FILESYSTEM, CommandBuilder.move_node(old_path, new_path)
    )
    CommandParser.move_node(await get_notification(notifications))
