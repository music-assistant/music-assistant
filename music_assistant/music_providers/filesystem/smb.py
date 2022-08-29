"""SMB filesystem provider for Music Assistant."""

import contextvars
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from smb.base import SharedFile

from music_assistant.helpers.util import get_ip_from_host
from music_assistant.models.enums import ProviderType

from .base import FileSystemItem, FileSystemProviderBase
from .helpers import AsyncSMB, get_absolute_path, get_relative_path


async def create_item(
    file_path: str, entry: SharedFile, root_path: str
) -> FileSystemItem:
    """Create FileSystemItem from smb.SharedFile."""

    rel_path = get_relative_path(root_path, file_path)
    abs_path = get_absolute_path(root_path, file_path)
    return FileSystemItem(
        name=entry.filename,
        path=rel_path,
        absolute_path=abs_path,
        is_file=not entry.isDirectory,
        is_dir=entry.isDirectory,
        checksum=str(int(entry.last_write_time)),
        file_size=entry.file_size,
    )


smb_conn_ctx = contextvars.ContextVar("smb_conn_ctx", default=None)


class SMBFileSystemProvider(FileSystemProviderBase):
    """Implementation of an SMB File System Provider."""

    _attr_name = "smb"
    _attr_type = ProviderType.FILESYSTEM_SMB
    _service_name = ""
    _root_path = "/"
    _remote_name = ""
    _target_ip = ""

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""
        # extract params from path
        if self.config.path.startswith("\\\\"):
            path_parts = self.config.path[2:].split("\\", 2)
        elif self.config.path.startswith("smb://"):
            path_parts = self.config.path[6:].split("/", 2)
        else:
            path_parts = self.config.path.split(os.sep)
        self._remote_name = path_parts[0]
        self._service_name = path_parts[1]
        if len(path_parts) > 2:
            self._root_path = os.sep + path_parts[2]

        default_target_ip = await get_ip_from_host(self._remote_name)
        self._target_ip = self.config.options.get("target_ip", default_target_ip)
        async with self._get_smb_connection():
            return True

    async def listdir(
        self,
        path: str,
        recursive: bool = False,
    ) -> AsyncGenerator[FileSystemItem, None]:
        """
        List contents of a given provider directory/path.

        Parameters:
            - path: path of the directory (relative or absolute) to list contents of.
              Empty string for provider's root.
            - recursive: If True will recursively keep unwrapping subdirectories (scandir equivalent).

        Returns:
            AsyncGenerator yielding FileSystemItem objects.

        """
        abs_path = get_absolute_path(self._root_path, path)
        async with self._get_smb_connection() as smb_conn:
            path_result: list[SharedFile] = await smb_conn.list_path(abs_path)
            for entry in path_result:
                if entry.filename.startswith("."):
                    # skip invalid/system files and dirs
                    continue
                file_path = os.path.join(path, entry.filename)
                item = await create_item(file_path, entry, self._root_path)
                if recursive and item.is_dir:
                    # yield sublevel recursively
                    try:
                        async for subitem in self.listdir(file_path, True):
                            yield subitem
                    except (OSError, PermissionError) as err:
                        self.logger.warning("Skip folder %s: %s", item.path, str(err))
                elif item.is_file or item.is_dir:
                    yield item

    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem."""
        abs_path = get_absolute_path(self._root_path, file_path)
        async with self._get_smb_connection() as smb_conn:
            entry: SharedFile = await smb_conn.get_attributes(abs_path)
        return FileSystemItem(
            name=file_path,
            path=get_relative_path(self._root_path, file_path),
            absolute_path=abs_path,
            is_file=not entry.isDirectory,
            is_dir=entry.isDirectory,
            checksum=str(int(entry.last_write_time)),
            file_size=entry.file_size,
        )

    async def exists(self, file_path: str) -> bool:
        """Return bool if this FileSystem musicprovider has given file/dir."""
        abs_path = get_absolute_path(self._root_path, file_path)
        async with self._get_smb_connection() as smb_conn:
            return await smb_conn.path_exists(abs_path)

    async def read_file_content(
        self, file_path: str, seek: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Yield (binary) contents of file in chunks of bytes."""
        abs_path = get_absolute_path(self._root_path, file_path)

        async with self._get_smb_connection() as smb_conn:
            async for chunk in smb_conn.retrieve_file(abs_path, seek):
                yield chunk

    async def write_file_content(self, file_path: str, data: bytes) -> None:
        """Write entire file content as bytes (e.g. for playlists)."""
        abs_path = get_absolute_path(self._root_path, file_path)
        async with self._get_smb_connection() as smb_conn:
            await smb_conn.write_file(abs_path, data)

    @asynccontextmanager
    async def _get_smb_connection(self) -> AsyncGenerator[AsyncSMB, None]:
        """Get instance of AsyncSMB."""

        if existing := smb_conn_ctx.get():
            yield existing
            return

        async with AsyncSMB(
            remote_name=self._remote_name,
            service_name=self._service_name,
            username=self.config.username,
            password=self.config.password,
            target_ip=self._target_ip,
            options=self.config.options,
        ) as smb_conn:
            token = smb_conn_ctx.set(smb_conn)
            yield smb_conn
        smb_conn_ctx.reset(token)
