# UF2 upload touches only the board's own port and mounts the drive on Linux

Status: implemented

## Context

`pio run -t upload` for the STM32 UF2 path (XIAO STM32C5) failed on a Linux
host without a desktop automounter: TinyUF2 enumerated `XIAOC5BOOT` within a
second, but `uf2upload.py` only accepted a mounted volume. Its fallback,
`mount` into `/mnt/uf2_upload`, needs root, and every error was swallowed,
so the drive wait always timed out with "UF2 drive not found".

On the same host the trigger was sent to an unrelated CH340 (`/dev/ttyUSB9`).
When the board is not enumerated as CDC (already in the bootloader, or
running firmware without USB), PlatformIO's `AutodetectUploadPort` falls back
to any known USB-serial bridge from its udev rules. `uf2upload.py` then
opened that port at 9600/1200 bps and, on failure, toggled DTR, which can
reset whatever board is attached. Without `--port` it also touched every
serial port on the system.

## Decision

- The trigger port is either the explicit `upload_port` or a port whose USB
  VID:PID matches the board's `build.hwids`, passed as `--hwid` by the family
  builder. If neither exists, no port is touched and the upload waits for the
  drive (double-tap RESET). Ports are never guessed.
- `stm32_build.py` calls `AutodetectUploadPort` only when `upload_port` is
  set (explicit or pattern); otherwise port resolution happens in the
  `uf2upload.py` child process, consistent with
  [upload-wait-interruptibility](2026-10-08-upload-wait-interruptibility.md).
- On Linux an unmounted drive is mounted with
  `udisksctl mount --no-user-interaction` (unprivileged via udisks2);
  `mount(8)` is attempted only as root. A failure is printed once with the
  underlying errors and a manual-mount hint.
- `uf2upload.py` stays chip-agnostic: the VID:PID values come from board
  metadata through the builder, not from tables in `builder/tools/`.
- Trigger ports are closed after discarding queued output
  (`reset_output_buffer()`). A freshly enumerated Linux tty starts with
  `ECHO`, so CDC output the application releases when DTR rises is echoed
  back before pyserial selects raw mode; firmware that never reads its CDC
  OUT endpoint (e.g. a console-only Zephyr app) leaves that echo queued, and
  `close()` blocked for the tty_port default `closing_wait` of 30 s. This is
  generic Linux tty/cdc-acm behavior, not distribution or kernel-config
  specific.

## Alternatives considered

- Filtering PlatformIO's autodetected port against the hwids in the builder:
  rejected — the autodetection itself waits in-process and still yields an
  unrelated port that must then be discarded.
- Requiring users to install an automounter (udiskie, udev rules): rejected
  as the only remedy; udisks2 is present on mainstream distributions and
  needs no extra setup.

## Consequences

- Hosts with many USB-serial adapters no longer see stray resets during an
  upload.
- A board running firmware without the CDC trigger is not reset by the
  uploader; the user double-taps RESET as the drive-wait message says.
- Headless Linux hosts without udisks2 still need a manual mount, but now get
  an explicit message instead of a silent timeout.

## Validation

- `pytest tests/test_uf2upload.py` (port selection, CH340 exclusion,
  udisksctl mount, one-time failure report).
- Hardware: XIAO STM32C5 on Linux without an automounter — upload from the
  application (CDC 1200-bps trigger, drive auto-mounted), from the bootloader
  with an unmounted drive (no port touched), and with the drive already
  mounted.

## Related files

- `builder/tools/uf2upload.py`
- `builder/board_build/stm32/stm32_build.py`
- `tests/test_uf2upload.py`
