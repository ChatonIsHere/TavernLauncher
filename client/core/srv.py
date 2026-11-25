"""
SRV lookups, so a server can be joined by name alone.

A host publishes one record per server, the way Minecraft uses _minecraft._tcp:

    _townshiptale._tcp.play.example.com.  SRV  0 5 27016 box.example.com.

That points at the server's auth port (TavernLib's 1762 by default). The
auth replies then carry the game and web ports, so the player types
"play.example.com" and nothing else, whatever ports the host chose.

Windows only (DnsQuery_W, the system resolver and its cache); elsewhere,
and for IP addresses, there is never a record.
"""
import ctypes
import ipaddress
import random
import sys

SERVICE = "_townshiptale._tcp."

_DNS_TYPE_SRV = 0x21
_DNS_QUERY_STANDARD = 0


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def pick(records):
    """(target, port) from [(priority, weight, port, target)] the RFC 2782
    way: the lowest priority, then a weighted random choice among those.
    None when there are none, or the only answer is "." (no service here)."""
    records = [r for r in records if r[3] not in ("", ".")]
    if not records:
        return None
    best = min(r[0] for r in records)
    group = [r for r in records if r[0] == best]
    total = sum(r[1] for r in group)
    if total:
        roll = random.uniform(0, total)
        for r in group:
            roll -= r[1]
            if roll <= 0:
                return r[3].rstrip("."), r[2]
    return group[0][3].rstrip("."), group[0][2]


def lookup(host):
    """(target, port) from host's SRV record, or None: no record, an IP
    address, not Windows, or the lookup failed."""
    host = (host or "").strip().rstrip(".")
    if not host or _is_ip(host) or sys.platform != "win32":
        return None
    try:
        return pick(_query(SERVICE + host))
    except Exception:
        return None


if sys.platform == "win32":
    from ctypes import wintypes as _wt

    class _SRV(ctypes.Structure):
        _fields_ = [("pNameTarget", _wt.LPWSTR), ("wPriority", _wt.WORD),
                    ("wWeight", _wt.WORD), ("wPort", _wt.WORD), ("Pad", _wt.WORD)]

    class _RECORD(ctypes.Structure):
        pass

    # Data is a union in windns.h; only its SRV member is read, and only for SRV records.
    _RECORD._fields_ = [("pNext", ctypes.POINTER(_RECORD)), ("pName", _wt.LPWSTR),
                        ("wType", _wt.WORD), ("wDataLength", _wt.WORD), ("Flags", _wt.DWORD),
                        ("dwTtl", _wt.DWORD), ("dwReserved", _wt.DWORD), ("Data", _SRV)]

    _dnsapi = ctypes.WinDLL("dnsapi")
    _dnsapi.DnsQuery_W.argtypes = [_wt.LPCWSTR, _wt.WORD, _wt.DWORD, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.POINTER(_RECORD)), ctypes.c_void_p]
    _dnsapi.DnsQuery_W.restype = ctypes.c_long
    _dnsapi.DnsRecordListFree.argtypes = [ctypes.POINTER(_RECORD), ctypes.c_int]
    _dnsapi.DnsRecordListFree.restype = None

    def _query(name):
        """[(priority, weight, port, target)]; [] when the name has none."""
        head = ctypes.POINTER(_RECORD)()
        if _dnsapi.DnsQuery_W(name, _DNS_TYPE_SRV, _DNS_QUERY_STANDARD, None, ctypes.byref(head), None):
            return []
        try:
            found, node = [], head
            while node:
                r = node.contents
                if r.wType == _DNS_TYPE_SRV:
                    d = r.Data
                    found.append((d.wPriority, d.wWeight, d.wPort, d.pNameTarget or ""))
                node = r.pNext
            return found
        finally:
            _dnsapi.DnsRecordListFree(head, 1)   # DnsFreeRecordList
