import sys
import typing
import asyncio
import logging
import argparse
import coloredlogs
import async_timeout

import zigpy_znp.types as t
import zigpy_znp.commands as c

from zigpy_znp.api import ZNP
from zigpy_znp.config import CONFIG_SCHEMA

coloredlogs.install(level=logging.DEBUG)
logging.getLogger("zigpy_znp").setLevel(logging.DEBUG)

LOGGER = logging.getLogger(__name__)


def compute_crc16(data: bytes) -> int:
    poly = 0x1021
    crc = 0x0000

    for byte in data:
        for _ in range(8):
            msb = 1 if (crc & 0x8000) else 0

            crc <<= 1
            crc &= 0xFFFF

            if byte & 0x80:
                crc |= 0x0001

            if msb:
                crc ^= poly

            byte <<= 1

    return crc


def get_firmware_crcs(firmware: bytes) -> typing.Tuple[int, int]:
    # There is room for *two* CRCs in the firmware file: the expected and the computed
    firmware_wihout_crcs = (
        firmware[: c.ubl.IMAGE_CRC_OFFSET]
        + firmware[c.ubl.IMAGE_CRC_OFFSET + 4 :]
        + b"\x00\x00"
    )

    # We only use the first one. The second one is written by the bootloader into flash.
    real_crc = int.from_bytes(
        firmware[c.ubl.IMAGE_CRC_OFFSET : c.ubl.IMAGE_CRC_OFFSET + 2], "little"
    )

    return real_crc, compute_crc16(firmware_wihout_crcs)


async def write_firmware(firmware: bytes, radio_path: str):
    if len(firmware) != c.ubl.IMAGE_SIZE:
        raise ValueError(
            f"Firmware is the wrong size."
            f" Expected {c.ubl.IMAGE_SIZE}, got {len(firmware)}"
        )

    expected_crc, computed_crc = get_firmware_crcs(firmware)

    if expected_crc != computed_crc:
        raise ValueError(
            f"Firmware CRC is incorrect. "
            f"Expected 0x{expected_crc:04X}, got 0x{computed_crc:04X}"
        )

    znp = ZNP(CONFIG_SCHEMA({"device": {"path": radio_path}}))

    # The bootloader handshake must be the very first command
    await znp.connect(test_port=False)

    try:
        async with async_timeout.timeout(5):
            handshake_rsp = await znp.request_callback_rsp(
                request=c.UBL.HandshakeReq.Req(),
                callback=c.UBL.HandshakeRsp.Callback(partial=True),
            )
    except asyncio.TimeoutError:
        raise RuntimeError(
            "Did not receive a bootloader handshake response!"
            " Make sure your adapter has just been plugged in and"
            " nothing else has had a chance to communicate with it."
        )

    if handshake_rsp.Status != c.ubl.BootloaderStatus.SUCCESS:
        raise RuntimeError(f"Bad bootloader handshake response: {handshake_rsp}")

    # All reads and writes are this size
    buffer_size = handshake_rsp.BufferSize

    for offset in range(0, c.ubl.IMAGE_SIZE, buffer_size):
        address = offset // c.ubl.FLASH_WORD_SIZE
        LOGGER.info("Write progress: %0.2f%%", (100.0 * offset) / c.ubl.IMAGE_SIZE)

        write_rsp = await znp.request_callback_rsp(
            request=c.UBL.WriteReq.Req(
                FlashWordAddr=address,
                Data=t.TrailingBytes(firmware[offset : offset + buffer_size]),
            ),
            callback=c.UBL.WriteRsp.Callback(partial=True),
        )

        assert write_rsp.Status == c.ubl.BootloaderStatus.SUCCESS

    # Now we have to read it all back
    for offset in range(0, c.ubl.IMAGE_SIZE, buffer_size):
        address = offset // c.ubl.FLASH_WORD_SIZE
        LOGGER.info(
            "Verification progress: %0.2f%%", (100.0 * offset) / c.ubl.IMAGE_SIZE
        )

        read_rsp = await znp.request_callback_rsp(
            request=c.UBL.ReadReq.Req(FlashWordAddr=address,),
            callback=c.UBL.ReadRsp.Callback(partial=True),
        )

        assert read_rsp.Status == c.ubl.BootloaderStatus.SUCCESS
        assert read_rsp.FlashWordAddr == address
        assert read_rsp.Data == firmware[offset : offset + buffer_size]

    # This seems to cause the bootloader to compute and verify the CRC
    enable_rsp = await znp.request_callback_rsp(
        request=c.UBL.EnableReq.Req(), callback=c.UBL.EnableRsp.Callback(partial=True),
    )

    assert enable_rsp.Status == c.ubl.BootloaderStatus.SUCCESS


async def main(argv):
    parser = argparse.ArgumentParser(description="Write firmware to a radio")
    parser.add_argument("serial", type=argparse.FileType("rb"), help="Serial port path")
    parser.add_argument(
        "--input",
        "-i",
        type=argparse.FileType("rb"),
        help="Input .bin file",
        required=True,
    )

    args = parser.parse_args(argv)

    # We just want to make sure it exists
    args.serial.close()

    await write_firmware(args.input.read(), args.serial.name)

    LOGGER.info("Unplug your adapter to leave bootloader mode!")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))  # pragma: no cover