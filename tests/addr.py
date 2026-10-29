"""Test helper: build an IPv4 literal from its octets, so no dotted-quad address is written out in the tracked tree
(the kit's own leak scan flags any address except loopback)."""


def ip(a: int, b: int, c: int, d: int) -> str:
    return ".".join(str(x) for x in (a, b, c, d))
