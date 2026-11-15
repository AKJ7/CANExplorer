"""RequestFileTransfer (UDS 0x38): add, replace, delete, read and resume files and read directories on an ECU with
a file system, with the data moved by TransferData (0x36) and RequestTransferExit (0x37) as for a download.

Request: modeOfOperation, filePathAndNameLength (2), filePathAndName (ASCII), then a dataFormatIdentifier (not for
delete and read directory) and, for add/replace/resume, fileSizeParameterLength (1) with the uncompressed and the
compressed size in that width. Response: the mode, then (not for delete) lengthFormatIdentifier — the byte count of
maxNumberOfBlockLength — and maxNumberOfBlockLength, the dataFormatIdentifier, for read file/directory
fileSizeOrDirInfoParameterLength (2) with the uncompressed size or directory information length (and for read
file the compressed size), and for resume the 8-byte filePosition. Checked against udsoncan and Wireshark's UDS
dissector, which agree.

`transfer_blocks` and `receive_blocks` are the TransferData loops, shared with flashing (core/flashing.py).
"""

from dataclasses import dataclass
from typing import Callable, Optional

from can_explorer.core.uds import UdsError

SID = 0x38
ADD_FILE = 0x01
DELETE_FILE = 0x02
REPLACE_FILE = 0x03
READ_FILE = 0x04
READ_DIRECTORY = 0x05
RESUME_FILE = 0x06
MODES = {
    ADD_FILE: 'addFile',
    DELETE_FILE: 'deleteFile',
    REPLACE_FILE: 'replaceFile',
    READ_FILE: 'readFile',
    READ_DIRECTORY: 'readDir',
    RESUME_FILE: 'resumeFile',
}
ISOTP_MAX_PAYLOAD = 0xFFF  # A classic ISO-TP message (FD/escape lengths aside); also the flashing limit

Progress = Optional[Callable[[int, int], None]]  # (bytes done, total)


@dataclass(frozen=True, slots=True)
class FileTransferResponse:
    mode: int
    max_block_length: Optional[int] = None  # Including the TransferData SID and block counter
    data_format: Optional[int] = None
    file_size: Optional[int] = None  # readFile: uncompressed size; readDir: directory information length
    compressed_size: Optional[int] = None
    file_position: Optional[int] = None  # resumeFile

    @property
    def mode_name(self) -> str:
        return MODES.get(self.mode, f'mode 0x{self.mode:02X}')


def file_transfer_request(
    mode: int, path: str, data_format: int = 0x00, size: Optional[int] = None, compressed_size: Optional[int] = None
) -> bytes:
    if mode not in MODES:
        raise UdsError(f'Unknown RequestFileTransfer mode 0x{mode:02X}')
    try:
        name = path.encode('ascii')
    except UnicodeEncodeError:
        raise UdsError('File paths are ASCII') from None
    if not name or len(name) > 0xFFFF:
        raise UdsError('The file path must have 1-65535 characters')
    payload = bytes([SID, mode]) + len(name).to_bytes(2, 'big') + name
    if mode not in (DELETE_FILE, READ_DIRECTORY):
        payload += bytes([data_format])
    if mode in (ADD_FILE, REPLACE_FILE, RESUME_FILE):
        if size is None:
            raise UdsError(f'{MODES[mode]} needs the file size')
        compressed = size if compressed_size is None else compressed_size
        width = max(1, (max(size, compressed).bit_length() + 7) // 8)
        payload += bytes([width]) + size.to_bytes(width, 'big') + compressed.to_bytes(width, 'big')
    return payload


def parse_file_transfer_response(response: bytes) -> FileTransferResponse:
    if len(response) < 2 or response[0] != SID + 0x40:
        raise UdsError('Not a RequestFileTransfer response')
    mode = response[1]
    if mode == DELETE_FILE:
        return FileTransferResponse(mode)
    offset = 2

    def take(count: int, what: str) -> bytes:
        nonlocal offset
        if len(response) < offset + count:
            raise UdsError(f'RequestFileTransfer response ends before its {what}')
        chunk = response[offset : offset + count]
        offset += count
        return chunk

    length_bytes = take(1, 'lengthFormatIdentifier')[0]
    if not 1 <= length_bytes <= 8:
        raise UdsError(f'RequestFileTransfer response with a {length_bytes}-byte block length')
    max_block = int.from_bytes(take(length_bytes, 'maxNumberOfBlockLength'), 'big')
    data_format = take(1, 'dataFormatIdentifier')[0]
    size = compressed = position = None
    if mode in (READ_FILE, READ_DIRECTORY):
        width = int.from_bytes(take(2, 'fileSizeOrDirInfoParameterLength'), 'big')
        if not 1 <= width <= 8:
            raise UdsError(f'RequestFileTransfer response with {width}-byte file sizes')
        size = int.from_bytes(take(width, 'file size'), 'big')
        if mode == READ_FILE:
            compressed = int.from_bytes(take(width, 'compressed file size'), 'big')
    if mode == RESUME_FILE:
        position = int.from_bytes(take(8, 'filePosition'), 'big')
    return FileTransferResponse(mode, max_block, data_format, size, compressed, position)


def _chunk_size(max_block: int, limit: int = 0) -> int:
    block = min(max_block, ISOTP_MAX_PAYLOAD)
    if limit:
        block = min(block, limit)
    if block - 2 < 1:
        raise UdsError(f'The ECU accepts blocks of {max_block} bytes, too short for data')
    return block - 2  # The SID and the block counter


async def transfer_blocks(
    client, data: bytes, max_block: int, limit: int = 0, progress: Progress = None, exit_transfer: bool = True
) -> int:
    """Send `data` in TransferData blocks of at most `max_block` bytes (with SID and counter; `limit` caps it), the
    counter from 1 wrapping from 0xFF to 0x00, then RequestTransferExit. Returns the data bytes per block."""
    chunk = _chunk_size(max_block, limit)
    counter = 1
    for offset in range(0, len(data), chunk):
        block = data[offset : offset + chunk]
        response = await client.call(bytes([0x36, counter]) + block)
        if len(response) < 2 or response[1] != counter:
            raise UdsError(
                f'TransferData answered block {response[1] if len(response) > 1 else "?"}, expected {counter}'
            )
        if progress is not None:
            progress(offset + len(block), len(data))
        counter = (counter + 1) & 0xFF
    if exit_transfer:
        await client.call(bytes([0x37]))
    return chunk


async def receive_blocks(client, size: Optional[int], progress: Progress = None, max_blocks: int = 1 << 20) -> bytes:
    """Request TransferData blocks (an upload) until `size` bytes arrived — or, with `size` None, until the ECU
    answers with an empty block or refuses the next one — then RequestTransferExit."""
    data = bytearray()
    counter = 1
    for _ in range(max_blocks):
        if size is not None and len(data) >= size:
            break
        try:
            response = await client.call(bytes([0x36, counter]))
        except UdsError:
            if size is None and data:
                break  # The ECU has no more blocks
            raise
        if len(response) < 2 or response[1] != counter:
            raise UdsError(
                f'TransferData answered block {response[1] if len(response) > 1 else "?"}, expected {counter}'
            )
        if len(response) == 2 and size is None:
            break
        data += response[2:]
        if progress is not None:
            progress(len(data), size or len(data))
        counter = (counter + 1) & 0xFF
    await client.call(bytes([0x37]))
    return bytes(data[:size] if size is not None else data)


async def request_file_transfer(client, mode: int, path: str, **parameters) -> FileTransferResponse:
    response = parse_file_transfer_response(await client.call(file_transfer_request(mode, path, **parameters)))
    if response.mode != mode:
        raise UdsError(f'RequestFileTransfer answered {response.mode_name}, expected {MODES[mode]}')
    return response


async def write_file(
    client,
    path: str,
    data: bytes,
    replace: bool = False,
    data_format: int = 0x00,
    max_block_length: int = 0,
    progress: Progress = None,
) -> None:
    """Add (or replace) a file on the ECU. `data` is sent as given: with a dataFormatIdentifier other than 0x00 it
    must already be compressed/encrypted the way the ECU expects (that encoding is manufacturer specific)."""
    mode = REPLACE_FILE if replace else ADD_FILE
    response = await request_file_transfer(client, mode, path, data_format=data_format, size=len(data))
    if response.max_block_length is None:
        raise UdsError('RequestFileTransfer response without a block length')
    await transfer_blocks(client, data, response.max_block_length, max_block_length, progress)


async def read_file(client, path: str, data_format: int = 0x00, progress: Progress = None) -> bytes:
    """Read a file from the ECU (the data as the ECU sends it, compressed if it chose so)."""
    response = await request_file_transfer(client, READ_FILE, path, data_format=data_format)
    size = response.compressed_size if response.data_format else response.file_size
    return await receive_blocks(client, size, progress)


async def read_directory(client, path: str) -> bytes:
    """The directory information of `path`; its format is manufacturer specific, so it is returned as bytes."""
    response = await request_file_transfer(client, READ_DIRECTORY, path)
    return await receive_blocks(client, response.file_size)


async def delete_file(client, path: str) -> None:
    await request_file_transfer(client, DELETE_FILE, path)
