#!/usr/bin/env python3
"""Минимальный SNMPv2c GET/WALK на голом сокете, без зависимостей.

  python3 snmp-probe.py <host> <community> get  <OID> [OID...]
  python3 snmp-probe.py <host> <community> walk <OID>

Зачем, когда есть snmpwalk: PDU стенда видны не отовсюду. С операторской
машины они доступны только через VPN (и только после того, как партнёр
откроет адреса у себя), а с системной ВМ кластера — всегда, но net-snmp там
нет, и ставить пакеты на чужую прод-ВМ ради разовой сверки неправильно.
Этот файл решает ровно тот случай: копируется на ВМ, отвечает на вопрос,
удаляется.

Так 20.08.2026 были сверены OID'ы и единицы rPDU2 до того, как появился
сетевой доступ: мощность устройства — сотые кВт, энергия — десятые кВт·ч,
токи фаз и банков — десятые ампера.
"""
import socket
import sys


def enc_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag, val):
    return bytes([tag]) + enc_len(len(val)) + val


def enc_int(n):
    if n == 0:
        return tlv(0x02, b"\x00")
    b = n.to_bytes((n.bit_length() + 8) // 8, "big", signed=True)
    return tlv(0x02, b)


def enc_oid(oid):
    parts = [int(x) for x in oid.strip(".").split(".")]
    out = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 0x80:
            out += bytes([p])
            continue
        chunks = []
        while p:
            chunks.insert(0, (p & 0x7F) | 0x80)
            p >>= 7
        chunks[-1] &= 0x7F
        out += bytes(chunks)
    return tlv(0x06, out)


def parse_tlv(data, i=0):
    tag = data[i]
    ln = data[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(data[i:i + k], "big")
        i += k
    return tag, data[i:i + ln], i + ln


def dec_oid(b):
    parts = [b[0] // 40, b[0] % 40]
    cur = 0
    for byte in b[1:]:
        cur = (cur << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(cur)
            cur = 0
    return ".".join(str(p) for p in parts)


def dec_val(tag, b):
    if tag == 0x02 or tag in (0x41, 0x42, 0x43, 0x46):   # INTEGER/Counter/Gauge/TimeTicks
        return int.from_bytes(b, "big")
    if tag == 0x04:
        try:
            return b.decode("utf-8", "replace")
        except Exception:
            return b.hex()
    if tag == 0x05:
        return None
    if tag == 0x06:
        return dec_oid(b)
    if tag == 0x40:                                       # IpAddress
        return ".".join(str(x) for x in b)
    if tag == 0x80:
        return "НЕТ ТАКОГО OID (noSuchObject)"
    if tag == 0x81:
        return "НЕТ ЭКЗЕМПЛЯРА (noSuchInstance)"
    if tag == 0x82:
        return "КОНЕЦ ДЕРЕВА (endOfMibView)"
    return b.hex()


def request(host, community, oids, pdu_tag, req_id=1, timeout=4.0):
    vbs = b"".join(tlv(0x30, enc_oid(o) + tlv(0x05, b"")) for o in oids)
    pdu = tlv(pdu_tag, enc_int(req_id) + enc_int(0) + enc_int(0) + tlv(0x30, vbs))
    msg = tlv(0x30, enc_int(1) + tlv(0x04, community.encode()) + pdu)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    s.sendto(msg, (host, 161))
    data, _ = s.recvfrom(65535)
    s.close()
    _, body, _ = parse_tlv(data)
    _, _, i = parse_tlv(body)          # version
    _, _, i = parse_tlv(body, i)       # community
    tag, pdu_body, _ = parse_tlv(body, i)
    _, _, j = parse_tlv(pdu_body)      # request-id
    _, err, j = parse_tlv(pdu_body, j)
    _, _, j = parse_tlv(pdu_body, j)   # error-index
    if int.from_bytes(err, "big"):
        raise RuntimeError(f"SNMP error-status={int.from_bytes(err, 'big')}")
    _, vblist, _ = parse_tlv(pdu_body, j)
    out, k = [], 0
    while k < len(vblist):
        _, vb, k = parse_tlv(vblist, k)
        _, oid_b, m = parse_tlv(vb)
        vtag, vval, _ = parse_tlv(vb, m)
        out.append((dec_oid(oid_b), dec_val(vtag, vval)))
    return out


def main():
    if len(sys.argv) < 5 or sys.argv[3] not in ("get", "walk"):
        print(__doc__.strip())
        sys.exit(2)
    host, community, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    if mode == "get":
        for oid, val in request(host, community, sys.argv[4:], 0xA0):
            print(f"  {oid} = {val}")
        return
    root = sys.argv[4]
    cur, n = root, 0
    while n < 200:
        res = request(host, community, [cur], 0xA1)
        oid, val = res[0]
        if not oid.startswith(root.strip(".")) or "КОНЕЦ ДЕРЕВА" in str(val):
            break
        print(f"  {oid} = {val}")
        cur, n = oid, n + 1


if __name__ == "__main__":
    try:
        main()
    except socket.timeout:
        print("  ТАЙМАУТ: агент не ответил (SNMP выключен, другой community или фильтр)")
    except Exception as e:
        print(f"  ОШИБКА: {e}")
