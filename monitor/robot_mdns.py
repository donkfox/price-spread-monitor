from __future__ import annotations

import ipaddress
import logging
import select
import socket
import struct
import threading


MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
DEFAULT_MDNS_HOSTNAME = "xiaobang.local"
MDNS_ANSWER_TTL_SECONDS = 120
LAN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "172.16.0.0/12",
        "192.168.0.0/16",
    )
)
BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def local_private_ipv4() -> list[str]:
    lan_candidates: set[str] = set()
    fallback_candidates: set[str] = set()
    try:
        for item in socket.getaddrinfo(
            socket.gethostname(),
            None,
            socket.AF_INET,
        ):
            address = ipaddress.ip_address(item[4][0])
            if not address.is_private or address in BENCHMARK_NETWORK:
                continue
            if any(address in network for network in LAN_NETWORKS):
                lan_candidates.add(str(address))
            elif (
                not address.is_loopback
                and not address.is_link_local
                and not address.is_multicast
            ):
                fallback_candidates.add(str(address))
    except OSError:
        pass
    if lan_candidates:
        return sorted(lan_candidates)
    if fallback_candidates:
        return sorted(fallback_candidates)

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = str(probe.getsockname()[0])
    except OSError:
        address = "0.0.0.0"
    finally:
        probe.close()
    return [] if address == "0.0.0.0" else [address]


def encode_dns_name(name: str) -> bytes:
    normalized = name.rstrip(".").lower()
    if not normalized:
        return b"\x00"
    output = bytearray()
    for part in normalized.split("."):
        encoded = part.encode("ascii", "ignore")[:63]
        if not encoded:
            continue
        output.append(len(encoded))
        output.extend(encoded)
    output.append(0)
    return bytes(output)


def decode_dns_name(packet: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    if depth > 8:
        raise ValueError("DNS name compression loop")
    labels: list[str] = []
    while offset < len(packet):
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            suffix, _ = decode_dns_name(packet, pointer, depth + 1)
            if suffix:
                labels.append(suffix)
            return ".".join(labels), offset + 2
        offset += 1
        if length == 0:
            return ".".join(labels), offset
        if offset + length > len(packet):
            raise ValueError("truncated DNS label")
        labels.append(
            packet[offset : offset + length].decode("ascii", "ignore").lower()
        )
        offset += length
    raise ValueError("unterminated DNS name")


def parse_mdns_questions(packet: bytes) -> list[tuple[str, int, int]]:
    if len(packet) < 12:
        raise ValueError("short DNS packet")
    question_count = struct.unpack("!H", packet[4:6])[0]
    if question_count > 32:
        raise ValueError("too many DNS questions")
    offset = 12
    questions: list[tuple[str, int, int]] = []
    for _ in range(question_count):
        name, offset = decode_dns_name(packet, offset)
        if offset + 4 > len(packet):
            raise ValueError("truncated DNS question")
        qtype, qclass = struct.unpack("!HH", packet[offset : offset + 4])
        offset += 4
        questions.append((name, qtype, qclass))
    return questions


def build_mdns_response(
    transaction_id: int,
    questions: list[tuple[str, int, int]],
    answers: list[tuple[str, str]],
    *,
    ttl: int = MDNS_ANSWER_TTL_SECONDS,
) -> bytes:
    header = struct.pack(
        "!HHHHHH",
        transaction_id & 0xFFFF,
        0x8400,
        len(questions),
        len(answers),
        0,
        0,
    )
    payload = bytearray(header)
    for name, qtype, qclass in questions:
        payload.extend(encode_dns_name(name))
        payload.extend(struct.pack("!HH", qtype, qclass))
    for name, address in answers:
        payload.extend(encode_dns_name(name))
        payload.extend(struct.pack("!HHIH", 1, 1, ttl, 4))
        payload.extend(socket.inet_aton(address))
    return bytes(payload)


class RobotMdnsResponder:
    def __init__(
        self,
        hostname: str = DEFAULT_MDNS_HOSTNAME,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.hostname = (
            str(hostname).strip().rstrip(".").lower() or DEFAULT_MDNS_HOSTNAME
        )
        self.logger = logger or logging.getLogger("robot.mdns")
        self.addresses = local_private_ipv4()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> dict[str, object]:
        return {
            "running": self._running,
            "hostname": self.hostname,
            "addresses": list(self.addresses),
        }

    def start(self) -> bool:
        if self._running:
            return True
        if not self.addresses:
            self.logger.warning("没有找到可用的局域网 IP，mDNS 广播未启动")
            return False
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
            socket.IPPROTO_UDP,
        )
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", MDNS_PORT))
            membership = struct.pack(
                "4sl",
                socket.inet_aton(MDNS_GROUP),
                socket.INADDR_ANY,
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        except OSError as error:
            sock.close()
            self.logger.warning("mDNS 广播启动失败：%s", error)
            return False
        self._socket = sock
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name="robot-mdns",
            daemon=True,
        )
        self._thread.start()
        self._running = True
        self.logger.info(
            "mDNS 已广播：%s -> %s",
            self.hostname,
            ", ".join(self.addresses),
        )
        return True

    def stop(self) -> None:
        self._stop_event.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        self._running = False

    def _response_for(self, packet: bytes) -> bytes | None:
        questions = parse_mdns_questions(packet)
        if not questions:
            return None
        hostname = self.hostname.rstrip(".")
        wanted = [
            (name, qtype, qclass)
            for name, qtype, qclass in questions
            if name.rstrip(".") == hostname and qtype in (1, 255)
        ]
        if not wanted:
            return None
        transaction_id = struct.unpack("!H", packet[0:2])[0]
        return build_mdns_response(
            transaction_id,
            wanted,
            [(hostname, address) for address in self.addresses],
        )

    def _serve(self) -> None:
        while not self._stop_event.is_set():
            sock = self._socket
            if sock is None:
                break
            try:
                readable, _, _ = select.select([sock], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                data, source = sock.recvfrom(4096)
            except OSError:
                continue
            try:
                response = self._response_for(data)
            except ValueError:
                continue
            if response is None:
                continue
            try:
                sock.sendto(response, source)
            except OSError as error:
                self.logger.debug("mDNS 响应发送失败：%s", error)
