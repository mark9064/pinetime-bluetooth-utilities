import math
import sys
import functools
import os
import readline
import json
import zipfile
from typing import Union

import bleak
import anyio

import filesystem

DEVICE_NAME = "InfiniTime"


def fs_response(
    queue: anyio.streams.memory.MemoryObjectSendStream,
    _: bleak.BleakGATTCharacteristic,
    data: bytearray,
) -> None:
    queue.send_nowait(data)


async def load_resource_zip(
    client: bleak.BleakClient,
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
    path: str,
) -> None:
    files = {}
    with zipfile.ZipFile(path) as file:
        manifest = json.loads(file.read("resources.json"))
        for file_data in manifest["resources"]:
            files[file_data["filename"]] = file.read(file_data["filename"])
    ensured_paths = set()
    for file_data in manifest["resources"]:
        print("Uploading {0}".format(file_data["filename"]))
        path = file_data["path"]
        try:
            await filesystem.remove_node(client, notifications, path)
        except filesystem.FSError:
            pass
        to_ensure = []
        split_path = path
        while split_path != "/":
            split_path, _ = os.path.split(split_path)
            to_ensure.append(split_path)
        for subdir in to_ensure[::-1][1:]:
            if subdir not in ensured_paths:
                try:
                    await filesystem.make_directory(client, notifications, subdir)
                except filesystem.FSError:
                    pass
                ensured_paths.add(subdir)
        await filesystem.write_file(
            client, notifications, path, files[file_data["filename"]]
        )
    print("Done!")


Tree = dict[str, Union[filesystem.File, "Tree"]]


async def filesystem_tree(
    client: bleak.BleakClient,
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
) -> Tree:
    current_tree: Tree = {}
    to_explore = [("/",)]
    while to_explore:
        current = to_explore.pop(0)
        path = os.path.join(*current)
        listing = await filesystem.list_directory(client, notifications, path)
        subtree = current_tree
        for subdir in current[:-1]:
            subtree = subtree[subdir]
        current_tree_entry: dict[str, filesystem.File] = {}
        subtree[current[-1]] = current_tree_entry
        for name, node in listing.items():
            if isinstance(node, filesystem.File):
                current_tree_entry[name] = node
            else:
                if name in {".", ".."}:
                    continue
                to_explore.append((*current, name))
    return current_tree


def format_node(node: filesystem.Node) -> str:
    if isinstance(node, filesystem.Directory):
        return f"{node.name}: directory"
    if isinstance(node, filesystem.File):
        return f"{node.name}: file, {node.size}B"
    raise RuntimeError("Unknown node type")


def format_dir(directory: dict[str, filesystem.Node]) -> str:
    return "\n".join(
        format_node(node) for node in directory.values() if node.name not in {".", ".."}
    )


def format_tree(tree: Tree, indent_level: int = 0) -> str:
    out = []
    for name, entry in tree.items():
        if isinstance(entry, filesystem.File):
            out.append(indent_level * "  " + format_node(entry))
        else:
            out.append(indent_level * "  " + name)
            out.append(format_tree(entry, indent_level + 1))
    return "\n".join(out)


async def execute_command(
    args: list[str],
    client: bleak.BleakClient,
    notifications: anyio.streams.memory.MemoryObjectReceiveStream,
) -> None:
    if args[0] == "read":
        if len(args) not in {2, 3}:
            print("Usage: read <path on pinetime> [<path on local disk>]")
            return
        data = await filesystem.read_file(client, notifications, args[1])
        if len(args) == 2:
            print(bytes(data))
        else:
            open(args[2], "wb").write(data)
    elif args[0] == "write":
        if len(args) != 3:
            print("Usage: write <file on local disk> <path on pinetime>")
            return
        data = open(args[1], "rb").read()
        await filesystem.write_file(client, notifications, args[2], data)
    elif args[0] == "ls":
        if len(args) not in {1, 2}:
            print("Usage: ls [<path>]")
            return
        if len(args) == 1:
            args.append("/")
        print(
            format_dir(await filesystem.list_directory(client, notifications, args[1]))
        )
    elif args[0] == "rm":
        if len(args) != 2:
            print("Usage: rm <path>")
            return
        await filesystem.remove_node(client, notifications, args[1])
    elif args[0] == "mv":
        if len(args) != 3:
            print("Usage: mv <source> <dest>")
            return
        await filesystem.move(client, notifications, args[1], args[2])
    elif args[0] == "mkdir":
        if len(args) != 2:
            print("Usage: mkdir <path>")
            return
        await filesystem.make_directory(client, notifications, args[1])
    elif args[0] == "loadzip":
        if len(args) != 2:
            print("Usage: loadzip <path to resource zip>")
            return
        await load_resource_zip(client, notifications, args[1])
    elif args[0] == "tree":
        print(format_tree(await filesystem_tree(client, notifications)))


async def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        print("Specify commands as command line arguments")
        print("Commands: read, write, ls, rm, mv, mkdir, loadzip, tree, repl")
        print("e.g. python filesystem_cli.py loadzip </path/to/infinitime-resources.zip>")
        print("For command usage instructions, run repl")
        return

    print("Searching")
    device = await bleak.BleakScanner.find_device_by_name(DEVICE_NAME)
    if device is None:
        print("No devices found")
        return
    client = bleak.BleakClient(device)
    print("Connecting")
    await client.connect(timeout=15)
    print("Connected")
    receive_stream: anyio.streams.memory.MemoryObjectReceiveStream
    send_stream, receive_stream = anyio.create_memory_object_stream[bytearray](
        max_buffer_size=math.inf
    )
    await client.start_notify(
        filesystem.FILESYSTEM, functools.partial(fs_response, send_stream)
    )
    negotiated_mtu = client.services.get_characteristic(
        filesystem.FILESYSTEM
    ).max_write_without_response_size
    min_mtu = 220
    if negotiated_mtu < min_mtu:
        print(f"Reported MTU too small: {negotiated_mtu}, need >={min_mtu}")
        print("However the bluetooth stack is usually incorrect, continuing anyway...")

    if len(sys.argv) == 1 or sys.argv[1] == "repl":
        print("REPL mode, 'exit' to exit")
        print("Commands: read, write, ls, rm, mv, mkdir, loadzip, tree")
        print("Run a command with no arguments for usage")
        while True:
            try:
                query = input(">>> ")
            except EOFError:
                break
            if query == "exit":
                break
            try:
                await execute_command(query.split(" "), client, receive_stream)
            except filesystem.FSError as exc:
                print(f"FS error: {exc}")

    else:
        try:
            await execute_command(sys.argv[1:], client, receive_stream)
        except filesystem.FSError as exc:
            print(f"FS error: {exc}")
    await client.disconnect()


if __name__ == "__main__":
    anyio.run(main)
