# PineTime Bluetooth utilities

Scripts for firmware updates and managing resources on the external filesystem/flash in InfiniTime. See the sections below for usage.

Tested on Linux, should support most operating systems. Requires a working Bluetooth adapter to be available ;)

## Install

Clone the repository

Install the dependencies with `pip install -r requirements.txt`

You probably want to install the dependencies in a virtual environment (venv). If doing this, activate the virtual environment before installing dependencies.

## DFU (device firmware update)

On Linux with a Realtek Bluetooth chipset DFU will be slow due to a kernel bug. Other chipset manufacturers and operating systems should not be affected.

`python dfu.py </path/to/pinetime-mcuboot-app-dfu.zip>`

## Filesystem resources

For interactive usage (e.g. browsing filesystem, adding/removing/moving files)

`python filesystem_cli.py repl`

If you only want to load a resource zip

`python filesystem_cli.py loadzip </path/to/infinitime-resources.zip>`
