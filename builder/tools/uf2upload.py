#!/usr/bin/env python3
"""
UF2 upload tool for TinyUF2 bootloader.

Detects the UF2 mass storage device, optionally triggers bootloader mode
via serial 1200-baud touch (for boards with USB CDC support), and writes
the .uf2 firmware file to the virtual FAT drive.

Supports: Linux, Windows, macOS
"""

import os
import sys
import time
import errno
import shutil
import argparse
import subprocess
import platform

# ---------------------------------------------------------------------------
# UF2 drive detection
# ---------------------------------------------------------------------------

UF2_VOLUME_LABEL = "XIAOC5BOOT"
UF2_DRIVE_TIMEOUT = 30  # seconds to wait for drive to appear
SERIAL_BUSY_TIMEOUT = 60  # seconds to wait for a busy serial port to be released

_reported_mount_failures = set()


def set_volume_label(label):
    """Override the default volume label for UF2 drive detection."""
    global UF2_VOLUME_LABEL
    UF2_VOLUME_LABEL = label


def _linux_mountpoint(dev):
    """Return the first mount point of a block device, or None."""
    try:
        result = subprocess.run(
            ["lsblk", "-no", "MOUNTPOINT", dev],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    for mount in result.stdout.splitlines():
        mount = mount.strip()
        if mount and os.path.isdir(mount):
            return mount
    return None


def _mount_linux(dev):
    """Mount a block device and return its mount point, or None.

    Systems without a desktop automounter leave the bootloader drive
    unmounted. udisks2 lets an unprivileged user mount removable media,
    so it is tried first; a plain mount(8) needs root and is only tried
    as root. A failure is reported once per device instead of silently
    timing out the drive wait.
    """
    errors = []
    if shutil.which("udisksctl"):
        try:
            result = subprocess.run(
                ["udisksctl", "mount", "-b", dev, "--no-user-interaction"],
                capture_output=True, text=True, timeout=15
            )
            mount = _linux_mountpoint(dev)
            if mount:
                return mount
            errors.append("udisksctl: " + (result.stderr or result.stdout).strip())
        except Exception as exc:
            errors.append(f"udisksctl: {exc}")
    else:
        errors.append("udisksctl not found")

    if os.geteuid() == 0:
        mnt = "/mnt/uf2_upload"
        try:
            os.makedirs(mnt, exist_ok=True)
            result = subprocess.run(["mount", dev, mnt],
                                    capture_output=True, text=True, timeout=10)
            if os.path.ismount(mnt):
                return mnt
            errors.append("mount: " + result.stderr.strip())
        except Exception as exc:
            errors.append(f"mount: {exc}")

    if dev not in _reported_mount_failures:
        _reported_mount_failures.add(dev)
        print(f"\n  Found '{UF2_VOLUME_LABEL}' at {dev}, but it is not mounted "
              f"and could not be mounted:")
        for error in errors:
            print(f"    {error}")
        print(f"  Mount it manually (e.g. 'udisksctl mount -b {dev}') "
              f"while this upload is waiting.")
    return None


def _find_uf2_drive_linux():
    """Find UF2 drive on Linux using /dev/disk/by-label or lsblk."""
    # Method 1: /dev/disk/by-label/
    by_label = "/dev/disk/by-label"
    if os.path.isdir(by_label):
        for entry in os.listdir(by_label):
            if entry.upper() == UF2_VOLUME_LABEL.upper():
                dev = os.path.realpath(os.path.join(by_label, entry))
                mount = _linux_mountpoint(dev) or _mount_linux(dev)
                if mount:
                    return mount

    # Method 2: lsblk
    try:
        result = subprocess.run(
            ["lsblk", "-no", "LABEL,MOUNTPOINT", "-l"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].upper() == UF2_VOLUME_LABEL.upper():
                mount = parts[1].strip()
                if mount and os.path.isdir(mount):
                    return mount
    except Exception:
        pass

    return None


def _find_uf2_drive_windows():
    """Find UF2 drive on Windows using powershell."""
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "(Get-WmiObject Win32_LogicalDisk | "
             "Where-Object { $_.VolumeName -eq '" + UF2_VOLUME_LABEL
             + "' }).DeviceID"],
            capture_output=True, text=True, timeout=10
        )
        drive = result.stdout.strip()
        if drive and os.path.isdir(drive):
            return drive
    except Exception:
        pass
    return None


def _find_uf2_drive_macos():
    """Find UF2 drive on macOS."""
    mnt = "/Volumes/" + UF2_VOLUME_LABEL
    if os.path.isdir(mnt):
        return mnt
    return None


def find_uf2_drive():
    """Find the UF2 mass storage device by volume label."""
    system = platform.system()
    if system == "Linux":
        return _find_uf2_drive_linux()
    elif system == "Windows":
        return _find_uf2_drive_windows()
    elif system == "Darwin":
        return _find_uf2_drive_macos()
    return None


def wait_for_uf2_drive(timeout=UF2_DRIVE_TIMEOUT):
    """Wait for UF2 drive to appear, return path or None."""
    start = time.time()
    dots = 0
    while time.time() - start < timeout:
        drive = find_uf2_drive()
        if drive:
            return drive
        time.sleep(0.5)
        dots += 1
        if dots % 4 == 0:
            sys.stdout.write(".\n")
            sys.stdout.flush()
        else:
            sys.stdout.write(".")
            sys.stdout.flush()
    return None


# ---------------------------------------------------------------------------
# Bootloader trigger
# ---------------------------------------------------------------------------

def _classify_open_error(exc):
    """Map a serial open exception to "busy", "denied", or an error text.

    Windows open failures carry no errno, so match the WinError 5 text
    there (the exception class name survives a localized message). POSIX
    chains the original OSError, where only EBUSY means busy: EACCES is
    a permissions problem (user not in the dialout group, or a macOS
    privacy denial) and must fail fast instead of being waited on as if
    a monitor held the port.
    """
    if platform.system() == "Windows":
        text = str(exc)
        if "PermissionError" in text or "Access is denied" in text:
            return "busy"
        return text
    cause = exc.__cause__ or exc.__context__
    err = getattr(cause, "errno", None)
    if err == errno.EBUSY:
        return "busy"
    if err == errno.EACCES:
        return "denied"
    return str(exc)


def _discard_and_close(s):
    """Close a trigger port without waiting for queued output to drain.

    A freshly enumerated POSIX tty starts with echo enabled, and the
    application's CDC output (released when DTR rises on open) arrives
    before pyserial switches the port to raw mode. That output is echoed
    back to the device; firmware that never reads its CDC OUT endpoint
    leaves the echo stuck, and close() then blocks for the port's
    closing_wait (30 s by default). Nothing queued here is meant to be
    delivered, so drop it first.
    """
    try:
        s.reset_output_buffer()
    except Exception:
        pass
    s.close()


def open_close_port(port, baudrate):
    """Open and close a serial port briefly, changing its line coding.

    Returns None on success, "busy" when another program holds the
    port open, "denied" when the user lacks permission to open it
    (POSIX), or the open error text for other failures.
    """
    import serial
    try:
        s = serial.Serial(port, baudrate)
    except Exception as exc:
        return _classify_open_error(exc)
    try:
        _discard_and_close(s)
    except Exception:
        pass
    return None


def wait_for_port_release(port, timeout=SERIAL_BUSY_TIMEOUT):
    """Wait for a busy serial port to be released or the UF2 drive to appear.

    Returns "port" when the port became available, "drive" when the UF2
    drive appeared (the board is entering the bootloader on its own),
    or None on timeout.
    """
    start = time.time()
    while time.time() - start < timeout:
        if find_uf2_drive():
            return "drive"
        if open_close_port(port, 9600) is None:
            return "port"
        time.sleep(1)
    return None


def trigger_bootloader_1200baud(port):
    """Trigger bootloader via 1200 baud touch.

    This is the standard mechanism used by CircuitPython, Adafruit UF2,
    and Arduino boards. When the application firmware detects a 1200 baud
    connection, it writes the double-tap magic to RAM and resets into
    bootloader mode.

    Works only if the application firmware implements USB CDC with
    1200-baud detection.

    Returns (True, None) on success, (False, "busy") when another program
    holds the port open (detected at the 9600-bps prime below — by the
    time the 1200-bps request runs, the port is known to be free),
    (False, "denied") when the user lacks permission to open the port,
    or (False, None) when the board reset during the touch: the application
    reacts to the 1200-bps line coding within milliseconds, so any error
    from that point on usually means the device detached underneath us
    because the trigger worked. The caller's drive wait decides.
    """
    import serial

    # Open at a neutral baud first: usbser only issues SET_LINE_CODING
    # when the rate actually changes, so this both guarantees the later
    # 1200-bps request is really transmitted (a previous failed touch may
    # have left the device sitting at 1200) and probes whether another
    # program (serial monitor/terminal) holds the port open.
    result = open_close_port(port, 9600)
    if result is not None:
        return False, result

    try:
        # Open at 1200 baud - this is the "magic" signal. The line coding
        # request is sent when the port opens; no DTR dance, as the app
        # resets within milliseconds and anything after that races a
        # detaching device.
        s = serial.Serial(port, 1200)
        time.sleep(0.1)
        _discard_and_close(s)
        return True, None
    except Exception:
        # Device detached mid-touch (or right after): the trigger almost
        # certainly landed. Fall through to the drive wait.
        return False, None


def trigger_bootloader_dtr_double_tap(port):
    """Trigger bootloader via DTR double-tap simulation.

    For boards where the USB-serial chip's DTR is connected to the MCU's
    NRST pin (e.g. via a capacitor). Toggles DTR twice within the 500ms
    double-tap window of TinyUF2.

    Returns (True, None) on success or (False, reason).
    """
    import serial

    try:
        s = serial.Serial(port, 115200)
        # First reset
        s.dtr = False
        time.sleep(0.05)
        s.dtr = True
        # Wait within the 500ms double-tap window
        time.sleep(0.2)
        # Second reset (double-tap)
        s.dtr = False
        time.sleep(0.05)
        s.dtr = True
        time.sleep(0.1)
        _discard_and_close(s)
        return True, None
    except Exception as exc:
        return False, _classify_open_error(exc)


def trigger_bootloader(port):
    """Try all bootloader trigger methods on a serial port.

    Returns (True, None) on success, (False, "busy") when the port stayed
    busy (the caller should fail fast with an actionable message),
    (False, "denied") when the user lacks permission to open the port
    at all (the caller should fail fast — waiting cannot help), or
    (False, None) when no method succeeded for other reasons — the board
    may still be rebooting into the bootloader, so the caller keeps
    waiting for the drive.
    """
    # Method 1: 1200 baud touch (standard UF2 mechanism)
    ok, reason = trigger_bootloader_1200baud(port)
    if ok:
        return True, None
    if reason is None:
        # The board reset during the touch — nothing left to trigger.
        return False, None
    if reason == "denied":
        print(f"Error: no permission to open {port}.")
        print("  On Linux, add yourself to the 'dialout' group and log out/in;")
        print("  on macOS, allow USB-serial devices in System Settings >")
        print("  Privacy & Security. Then retry the upload.")
        return False, "denied"
    if reason == "busy":
        print(f"\n  Port {port} is held by another program "
              f"(serial monitor/terminal?).")
        print(f"  Waiting up to {SERIAL_BUSY_TIMEOUT}s for it to be released "
              f"(or for the UF2 drive to appear)...")
        outcome = wait_for_port_release(port, SERIAL_BUSY_TIMEOUT)
        if outcome == "drive":
            return True, None
        if outcome == "port":
            ok, retry_reason = trigger_bootloader_1200baud(port)
            if ok:
                return True, None
            if retry_reason is None:
                # The touch landed; the board reset underneath it.
                return False, None
        return False, "busy"

    # A non-busy failure usually means the port vanished mid-touch, i.e.
    # the board is already rebooting into the bootloader. Try the DTR
    # fallback and let the caller's drive wait decide.
    print(f"  1200-bps touch on {port} did not succeed ({reason}).")
    ok, _ = trigger_bootloader_dtr_double_tap(port)
    return ok, None


# ---------------------------------------------------------------------------
# UF2 file copy
# ---------------------------------------------------------------------------

def write_uf2_to_drive(uf2_path, drive_path):
    """Write UF2 firmware to the bootloader drive using raw binary I/O.

    Uses raw chunked write instead of shutil.copy2 because TinyUF2's
    virtual FAT filesystem (ghostfat) does not support file metadata
    operations. shutil.copy2 fails with WinError 433 on Windows.

    The bootloader intercepts sector writes and processes UF2 blocks
    in real-time. When all blocks are received, the device resets
    automatically (drive disappears).
    """
    dest = os.path.join(drive_path, os.path.basename(uf2_path))
    chunk_size = 512  # Match UF2 block / FAT sector size

    try:
        with open(uf2_path, "rb") as src:
            with open(dest, "wb") as dst:
                while True:
                    chunk = src.read(chunk_size)
                    if not chunk:
                        break
                    dst.write(chunk)
                dst.flush()
                try:
                    os.fsync(dst.fileno())
                except OSError:
                    pass
        return True
    except OSError as e:
        # If the drive disappears after writing, the bootloader received
        # the firmware and is resetting - this is actually success.
        errno = getattr(e, "winerror", getattr(e, "errno", 0))
        if errno == 433:  # WinError 433: device does not exist
            # Check if the drive is gone (device reset = upload success)
            time.sleep(1)
            if not find_uf2_drive():
                return True
        print(f"  Write error: {e}")
        return False


def write_uf2_cmd(uf2_path, drive_path):
    """Fallback: use OS copy command for writing UF2 file."""
    dest = os.path.join(drive_path, os.path.basename(uf2_path))
    system = platform.system()

    try:
        if system == "Windows":
            # Use xcopy for better compatibility with virtual filesystems
            result = subprocess.run(
                ["cmd", "/c", "copy", "/B", "/Y", uf2_path, dest],
                capture_output=True, text=True, timeout=30
            )
            return result.returncode == 0
        elif system == "Linux":
            subprocess.run(["cp", uf2_path, dest], timeout=30, check=True)
            subprocess.run(["sync"], timeout=10)
            return True
        else:  # macOS
            subprocess.run(["cp", uf2_path, dest], timeout=30, check=True)
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main upload flow
# ---------------------------------------------------------------------------

def resolve_trigger_port(serial_port, hwids):
    """Pick the serial port to send the bootloader trigger to, or None.

    An explicit port is used as given. Otherwise only a port whose USB
    VID:PID matches one of ``hwids`` is eligible: touching or toggling
    DTR on an unrelated serial device can reset whatever board is
    attached to it, so no port is ever guessed.
    """
    if serial_port:
        return serial_port
    if not hwids:
        print("  No serial port or board VID:PID given; skipping the "
              "bootloader trigger.")
        return None
    ports = _find_ports_by_hwid(hwids)
    wanted = ", ".join(f"{vid:04X}:{pid:04X}" for vid, pid in hwids)
    if not ports:
        print(f"  No serial port with VID:PID {wanted} found "
              f"(the board may already be in bootloader mode).")
        return None
    if len(ports) > 1:
        print(f"  Several serial ports match VID:PID {wanted}: "
              f"{', '.join(ports)}; using {ports[0]}.")
        print("  Set upload_port to select a specific board.")
    return ports[0]


def upload_uf2(uf2_path, serial_port=None, timeout=UF2_DRIVE_TIMEOUT,
               hwids=None):
    """Complete UF2 upload flow.

    Args:
        uf2_path: path to .uf2 firmware file
        serial_port: optional serial port for bootloader trigger
        timeout: seconds to wait for UF2 drive
        hwids: (vid, pid) tuples used to find the trigger port when
            serial_port is not given

    Returns:
        True on success, False on failure
    """
    if not os.path.isfile(uf2_path):
        print(f"Error: UF2 file not found: {uf2_path}")
        return False

    print(f"UF2 Upload: {uf2_path}")
    print(f"  Volume label: {UF2_VOLUME_LABEL}")
    print(f"  Firmware size: {os.path.getsize(uf2_path)} bytes")

    # Step 1: Check if drive already exists
    drive = find_uf2_drive()
    if drive:
        print(f"  Found UF2 drive: {drive}")
    else:
        # Step 2: Try serial trigger
        serial_port = resolve_trigger_port(serial_port, hwids)
        if serial_port:
            print(f"  Triggering bootloader via {serial_port}...")
            _, reason = trigger_bootloader(serial_port)
            if reason == "busy":
                print(f"Error: {serial_port} is still held by another program.")
                print("  Close the serial monitor/terminal using it and retry,")
                print("  or double-tap RESET to enter bootloader mode.")
                return False
            if reason == "denied":
                # Reason and remedy were printed by trigger_bootloader;
                # waiting cannot fix a permissions problem.
                return False

        # Step 3: Wait for drive
        print(f"  Waiting for UF2 drive (double-tap RESET if needed, "
              f"timeout {timeout}s)...", end="")
        sys.stdout.flush()

        drive = wait_for_uf2_drive(timeout)
        if not drive:
            print("\nError: UF2 drive not found.")
            print(f"  Please double-tap the RESET button on your board "
                  f"to enter bootloader mode.")
            print(f"  The drive should appear as '{UF2_VOLUME_LABEL}'.")
            return False
        print(f"\n  Found UF2 drive: {drive}")

    # Step 4: Write .uf2 file (try raw I/O first, fallback to OS command)
    print("  Writing firmware...")
    if write_uf2_to_drive(uf2_path, drive):
        # Give the bootloader time to process and reset
        time.sleep(2)
        if not find_uf2_drive():
            # Drive disappeared = device reset = upload success
            print("  Upload complete! Board is restarting.")
        else:
            print("  Upload complete! Board will restart shortly.")
        return True

    # Fallback: try OS copy command
    print("  Raw write failed, trying OS copy command...")
    if write_uf2_cmd(uf2_path, drive):
        time.sleep(2)
        print("  Upload complete!")
        return True

    print("  Error: Failed to write UF2 file.")
    return False


def _find_ports_by_hwid(hwids):
    """Return serial ports whose USB VID:PID is in ``hwids``, sorted."""
    import serial.tools.list_ports
    return sorted(
        info.device for info in serial.tools.list_ports.comports()
        if (info.vid, info.pid) in hwids
    )


def _parse_hwid(value):
    """Parse a "VID:PID" string (hex, optional 0x prefix) into a tuple."""
    try:
        vid, pid = value.split(":")
        return int(vid, 16), int(pid, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid VID:PID '{value}' (expected e.g. 0x2886:0x00C5)")


def main():
    # Progress messages (busy-port notices, drive-wait dots) must appear
    # while the platform pipes our output: stdout is block-buffered when
    # not attached to a TTY, and without this the busy notice would only
    # surface when the process exits.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(
        description="Upload UF2 firmware to TinyUF2 bootloader"
    )
    parser.add_argument(
        "uf2_file",
        help="Path to .uf2 firmware file"
    )
    parser.add_argument(
        "-p", "--port", default=None,
        help="Serial port for bootloader trigger"
    )
    parser.add_argument(
        "--hwid", action="append", type=_parse_hwid, default=[],
        metavar="VID:PID",
        help="USB VID:PID of the board's application serial port, used to "
             "find the trigger port when --port is not given (repeatable)"
    )
    parser.add_argument(
        "--label", default=None,
        help="Volume label of UF2 drive (default: XIAOC5BOOT)"
    )
    parser.add_argument(
        "-t", "--timeout", type=int, default=UF2_DRIVE_TIMEOUT,
        help=f"Timeout in seconds (default: {UF2_DRIVE_TIMEOUT})"
    )
    parser.add_argument(
        "--trigger-only", action="store_true",
        help="Only trigger bootloader, do not upload"
    )

    args = parser.parse_args()

    if args.label:
        set_volume_label(args.label)

    if args.trigger_only:
        port = resolve_trigger_port(args.port, args.hwid)
        if port:
            print(f"Triggering bootloader via {port}...")
            trigger_bootloader(port)
        return

    success = upload_uf2(args.uf2_file, args.port or None, args.timeout,
                         args.hwid)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
