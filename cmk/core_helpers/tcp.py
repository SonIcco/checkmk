#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import copy
import logging
import socket
import ssl
from typing import Any, Final, List, Mapping, Optional, Tuple

import cmk.utils.debug
from cmk.utils import paths
from cmk.utils.agent_registration import UUIDLinkManager
from cmk.utils.encryption import decrypt_by_agent_protocol, TransportProtocol
from cmk.utils.exceptions import MKFetcherError
from cmk.utils.type_defs import AgentRawData, HostAddress, HostName

from ._base import verify_ipaddress
from .agent import AgentFetcher, DefaultAgentFileCache
from .type_defs import Mode


class TCPFetcher(AgentFetcher):
    def __init__(
        self,
        file_cache: DefaultAgentFileCache,
        *,
        family: socket.AddressFamily,
        address: Tuple[Optional[HostAddress], int],
        timeout: float,
        host_name: HostName,
        encryption_settings: Mapping[str, str],
        use_only_cache: bool,
    ) -> None:
        super().__init__(file_cache, logging.getLogger("cmk.helper.tcp"))
        self.family: Final = socket.AddressFamily(family)
        # json has no builtin tuple, we have to convert
        self.address: Final[Tuple[Optional[HostAddress], int]] = (address[0], address[1])
        self.timeout: Final = timeout
        self.host_name: Final = host_name
        self.encryption_settings: Final = encryption_settings
        self.use_only_cache: Final = use_only_cache
        self._opt_socket: Optional[socket.socket] = None

    @property
    def _socket(self) -> socket.socket:
        if self._opt_socket is None:
            raise MKFetcherError("Not connected")
        return self._opt_socket

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            + ", ".join(
                (
                    f"{type(self.file_cache).__name__}",
                    f"family={self.family!r}",
                    f"timeout={self.timeout!r}",
                    f"host_name={self.host_name!r}",
                    f"encryption_settings={self.encryption_settings!r}",
                    f"use_only_cache={self.use_only_cache!r}",
                )
            )
            + ")"
        )

    @classmethod
    def _from_json(cls, serialized: Mapping[str, Any]) -> "TCPFetcher":
        serialized_ = copy.deepcopy(dict(serialized))
        address: Tuple[Optional[HostAddress], int] = serialized_.pop("address")
        host_name = HostName(serialized_.pop("host_name"))
        return cls(
            DefaultAgentFileCache.from_json(serialized_.pop("file_cache")),
            address=address,
            host_name=host_name,
            **serialized_,
        )

    def to_json(self) -> Mapping[str, Any]:
        return {
            "file_cache": self.file_cache.to_json(),
            "family": self.family,
            "address": self.address,
            "timeout": self.timeout,
            "host_name": str(self.host_name),
            "encryption_settings": self.encryption_settings,
            "use_only_cache": self.use_only_cache,
        }

    def open(self) -> None:
        verify_ipaddress(self.address[0])
        self._logger.debug(
            "Connecting via TCP to %s:%d (%ss timeout)",
            self.address[0],
            self.address[1],
            self.timeout,
        )
        self._opt_socket = socket.socket(self.family, socket.SOCK_STREAM)
        try:
            self._socket.settimeout(self.timeout)
            self._socket.connect(self.address)
            self._socket.settimeout(None)
        except socket.error as e:
            self._socket.close()
            self._opt_socket = None

            if cmk.utils.debug.enabled():
                raise
            raise MKFetcherError("Communication failed: %s" % e)

    def close(self) -> None:
        self._logger.debug("Closing TCP connection to %s:%d", self.address[0], self.address[1])
        if self._socket is not None:
            self._socket.close()
        self._opt_socket = None

    def _fetch_from_io(self, mode: Mode) -> AgentRawData:
        if self.use_only_cache:
            raise MKFetcherError(
                "Got no data: No usable cache file present at %s" % self.file_cache.base_path
            )

        agent_data, protocol = self._get_agent_data()
        return self._validate_decrypted_data(self._decrypt(protocol, agent_data))

    def _get_agent_data(self) -> Tuple[AgentRawData, TransportProtocol]:
        try:
            raw_protocol = self._socket.recv(2, socket.MSG_WAITALL)
        except socket.error as e:
            raise MKFetcherError(f"Communication failed: {e}") from e

        protocol = self._detect_transport_protocol(raw_protocol)

        self._validate_protocol(protocol)

        if protocol is TransportProtocol.TLS:
            with self._wrap_tls() as ssock:
                agent_data = self._recvall(ssock)
            return AgentRawData(agent_data[2:]), self._detect_transport_protocol(agent_data[:2])

        return AgentRawData(self._recvall(self._socket, socket.MSG_WAITALL)), protocol

    def _detect_transport_protocol(self, raw_protocol: bytes) -> TransportProtocol:
        try:
            protocol = TransportProtocol(raw_protocol)
            self._logger.debug("Detected transport protocol: {protocol} ({raw_protocol!r})")
            return protocol
        except ValueError:
            raise MKFetcherError(f"Unknown transport protocol: {raw_protocol!r}")

    def _validate_protocol(self, protocol: TransportProtocol) -> None:
        if protocol is TransportProtocol.TLS:
            return

        enc_setting = self.encryption_settings["use_regular"]
        if enc_setting == "tls":
            raise MKFetcherError("Refused: TLS not supported by agent")

        if protocol is TransportProtocol.PLAIN:
            if enc_setting in ("disable", "allow"):
                return
            raise MKFetcherError(
                "Agent output is plaintext but encryption is enforced by configuration"
            )

        if enc_setting == "disable":
            raise MKFetcherError(
                "Agent output is encrypted but encryption is disabled by configuration"
            )

    def _wrap_tls(self) -> ssl.SSLSocket:
        controller_uuid = UUIDLinkManager(
            received_outputs_dir=paths.received_outputs_dir,
            data_source_dir=paths.data_source_push_agent_dir,
        ).get_uuid(self.host_name)

        if controller_uuid is None:
            raise MKFetcherError("Agent controller not registered")

        self._logger.debug("Reading data from agent via TLS socket")
        try:
            ctx = ssl.create_default_context(cafile=str(paths.root_cert_file))
            ctx.load_cert_chain(certfile=paths.site_cert_file)
            return ctx.wrap_socket(self._socket, server_hostname=str(controller_uuid))
        except ssl.SSLError as e:
            raise MKFetcherError("Error establishing TLS connection") from e

    def _recvall(self, sock: socket.socket, flags: int = 0) -> bytes:
        self._logger.debug("Reading data from agent")
        buffer: List[bytes] = []
        try:
            while True:
                data = sock.recv(4096, flags)
                if not data:
                    break
                buffer.append(data)
        except socket.error as e:
            if cmk.utils.debug.enabled():
                raise
            raise MKFetcherError("Communication failed: %s" % e)

        return b"".join(buffer)

    def _decrypt(self, protocol: TransportProtocol, output: AgentRawData) -> AgentRawData:
        if not output:
            return output  # nothing to to, validation will fail

        if protocol is TransportProtocol.PLAIN:
            return protocol.value + output  # bring back stolen bytes

        self._logger.debug("Try to decrypt output")
        try:
            return AgentRawData(
                decrypt_by_agent_protocol(
                    self.encryption_settings["passphrase"],
                    protocol,
                    output,
                )
            )
        except Exception as e:
            raise MKFetcherError("Failed to decrypt agent output: %s" % e) from e

    def _validate_decrypted_data(self, output: AgentRawData) -> AgentRawData:
        if not output:  # may be caused by xinetd not allowing our address
            raise MKFetcherError("Empty output from agent at %s:%d" % self.address)
        if len(output) < 16:
            raise MKFetcherError("Too short output from agent: %r" % output)
        return output
