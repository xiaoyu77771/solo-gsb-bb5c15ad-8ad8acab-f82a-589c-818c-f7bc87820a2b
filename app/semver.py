"""SemVer 2.0.0 版本与 npm 风格版本范围解析。

支持的范围语法（以空白与 ``||`` 切分为 OR，每段以空白切分为 AND）：

* 比较器：``>=1.2.3``、``<2.0.0``、``=1.2.3``
*  caret：``^1.2.3``、``^0.2.3``、``^0.0.3``
*  tilde：``~1.2.3``、``~1.2``、``~1``
*  X 范围：``1.x``、``1.2.*``、``*``、``""``（空 = 任意）
*  hyphen：``1.2.3 - 2.0.0``
*  预发布：只有当范围内某个比较器的 主.次.修 三元组与候选版本同三元组，
    或存在显式引用同三元组预发布版本时，候选预发布版本才满足范围。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Tuple

_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z-.]+))?(?:\+([0-9A-Za-z-.]+))?$"
)
_PARTIAL_RE = re.compile(r"^[vV]?(\d+)(?:\.(\d+|x|X|\*))?(?:\.(\d+|x|X|\*))?(?:-([0-9A-Za-z-.]+))?$")


class VersionError(ValueError):
    """版本字符串或范围字符串非法。"""


def _parse_prerelease(identifier: Optional[str]) -> Optional[Tuple]:
    if identifier is None:
        return None
    ids: List[object] = []
    for part in identifier.split("."):
        if part == "":
            raise VersionError(f"非法预发布标识: {identifier!r}")
        if part.isdigit() and re.fullmatch(r"0[0-9]+", part):
            raise VersionError(f"预发布数字标识不允许前导零: {identifier!r}")
        ids.append(int(part) if part.isdigit() else part)
    return tuple(ids)


def _cmp_prerelease(a: Optional[Tuple], b: Optional[Tuple]) -> int:
    if a is None and b is None:
        return 0
    if a is None:
        return 1  # 正式版 > 预发布版
    if b is None:
        return -1
    for x, y in zip(a, b):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return -1 if x < y else 1
        if isinstance(x, int):
            return -1  # 数字 < 字符串
        if isinstance(y, int):
            return 1
        return -1 if x < y else 1  # type: ignore[operator]
    if len(a) == len(b):
        return 0
    return -1 if len(a) < len(b) else 1


@dataclass(frozen=True)
class Version:
    """不可变的 SemVer 版本。"""

    major: int
    minor: int
    patch: int
    prerelease: Optional[Tuple] = None
    build: Optional[str] = None

    @staticmethod
    def parse(text: str) -> "Version":
        if not isinstance(text, str):
            raise VersionError(f"版本必须是字符串: {text!r}")
        match = _VERSION_RE.match(text.strip())
        if not match:
            raise VersionError(f"非法 SemVer 版本: {text!r}")
        major, minor, patch, pre, build = match.groups()
        for number in (major, minor, patch):
            if len(number) > 1 and number.startswith("0"):
                raise VersionError(f"版本号不允许前导零: {text!r}")
        pre_tuple = _parse_prerelease(pre)
        return Version(int(major), int(minor), int(patch), pre_tuple, build)

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease is not None

    @property
    def triple(self) -> Tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def _key(self) -> Tuple:
        return (self.major, self.minor, self.patch, self.prerelease is not None, self.prerelease or ())

    def __lt__(self, other: "Version") -> bool:
        ka, kb = self._key(), other._key()
        if ka[:3] != kb[:3]:
            return ka[:3] < kb[:3]
        return _cmp_prerelease(self.prerelease, other.prerelease) < 0

    def __le__(self, other: "Version") -> bool:
        return self == other or self < other

    def __gt__(self, other: "Version") -> bool:
        return other < self

    def __ge__(self, other: "Version") -> bool:
        return self == other or other < self

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease is not None:
            base += "-" + ".".join(str(p) for p in self.prerelease)
        if self.build is not None:
            base += "+" + self.build
        return base


@dataclass(frozen=True)
class _Comparator:
    """单个原子比较，op ∈ {>=, >, <=, <, =}。"""

    op: str
    version: Version
    # 该比较器是否显式携带预发布引用（用于预发布可见性判断）
    explicit_prerelease: bool = False


def _bump_major(version: Version) -> Tuple[int, int, int]:
    return (version.major + 1, 0, 0)


def _bump_minor(version: Version) -> Tuple[int, int, int]:
    return (version.major, version.minor + 1, 0)


def _parse_partial(token: str) -> Optional[Tuple[Optional[int], Optional[int], Optional[int], Optional[str]]]:
    """解析可能残缺的版本号，如 ``1`` / ``1.2`` / ``1.x`` / ``*``。"""
    token = token.strip()
    if token in ("", "*", "x", "X", "latest"):
        return (None, None, None, None)
    match = _PARTIAL_RE.match(token)
    if not match:
        return None

    def conv(raw: Optional[str]) -> Optional[int]:
        if raw is None or raw in ("x", "X", "*"):
            return None
        return int(raw)

    major, minor, patch, pre = match.groups()
    return (conv(major), conv(minor), conv(patch), pre)


def _expand_partial_comparators(token: str) -> List[_Comparator]:
    """把裸版本（X 范围）展开为 npm 风格的 >= / < 比较器集合（AND）。"""
    partial = _parse_partial(token)
    if partial is None:
        raise VersionError(f"非法版本片段: {token!r}")
    major, minor, patch, pre = partial
    if major is None:
        return []  # * 匹配一切
    if minor is None:
        return [
            _Comparator(">=", Version(major, 0, 0)),
            _Comparator("<", Version(*_bump_major(Version(major, 0, 0)))),
        ]
    if patch is None:
        lower = Version(major, minor, 0)
        return [_Comparator(">=", lower), _Comparator("<", Version(*_bump_minor(lower)))]
    # 完整版本：带预发布时等价于精确匹配
    version = Version(major, minor, patch, _parse_prerelease(pre))
    if pre is not None:
        return [_Comparator("=", version, explicit_prerelease=True)]
    return [_Comparator("=", version)]


def _caret_ceiling(
    major: int, minor: Optional[int], patch: Optional[int], pre: Optional[str]
) -> Version:
    if minor is None:
        # ^1.x := >=1.0.0 <2.0.0
        return Version(*_bump_major(Version(major, 0, 0)))
    if patch is None:
        if major != 0:
            return Version(*_bump_major(Version(major, 0, 0)))
        # ^0.x := >=0.0.0 <1.0.0
        return Version(1, 0, 0)
    if major != 0:
        return Version(*_bump_major(Version(major, minor, patch)))
    if minor != 0:
        return Version(0, minor + 1, 0)
    # ^0.0.3 := >=0.0.3 <0.0.4
    ceiling = Version(0, 0, patch + 1)
    if pre is not None:
        # ^0.0.3-alpha 的上界仍为 0.0.4，语义不变
        pass
    return ceiling


def _parse_caret(token: str) -> List[_Comparator]:
    partial = _parse_partial(token[1:])
    if partial is None:
        raise VersionError(f"非法 caret 范围: {token!r}")
    major, minor, patch, pre = partial
    if major is None:
        # ^* 等价于 *
        return []
    minor_v = minor if minor is not None else 0
    patch_v = patch if patch is not None else 0
    lower = Version(major, minor_v, patch_v, _parse_prerelease(pre))
    upper = _caret_ceiling(major, minor, patch, pre)
    return [
        _Comparator(">=", lower, explicit_prerelease=pre is not None),
        _Comparator("<", upper),
    ]


def _parse_tilde(token: str) -> List[_Comparator]:
    partial = _parse_partial(token[1:])
    if partial is None:
        raise VersionError(f"非法 tilde 范围: {token!r}")
    major, minor, patch, pre = partial
    if major is None:
        return []
    minor_v = minor if minor is not None else 0
    patch_v = patch if patch is not None else 0
    lower = Version(major, minor_v, patch_v, _parse_prerelease(pre))
    if minor is None:
        upper = Version(*_bump_major(lower))
    else:
        upper = Version(*_bump_minor(lower))
    return [
        _Comparator(">=", lower, explicit_prerelease=pre is not None),
        _Comparator("<", upper),
    ]


def _parse_comparator_token(token: str) -> List[_Comparator]:
    token = token.strip()
    if token in ("", "*", "x", "X", "latest"):
        return []
    if token.startswith("^"):
        return _parse_caret(token)
    if token.startswith("~"):
        return _parse_tilde(token)
    op_match = re.match(r"^(>=|<=|>|<|=)(.+)$", token)
    if op_match:
        op, raw = op_match.groups()
        partial = _parse_partial(raw)
        if partial is None:
            raise VersionError(f"非法比较器: {token!r}")
        major, minor, patch, pre = partial
        if major is None:
            # 如 >=* 没有实际约束
            return []
        # >1.2 / <=1 之类的残缺版本由 _expand_partial_comparators 归一
        if op == "=":
            return _expand_partial_comparators(raw)
        if minor is None:
            base = Version(major, 0, 0)
            if op == ">":
                return [_Comparator(">=", Version(major + 1, 0, 0))]
            if op == "<":
                return [_Comparator("<", base)]
            if op == ">=":
                return [_Comparator(">=", base)]
            if op == "<=":
                return [_Comparator("<", Version(major + 1, 0, 0))]
        if patch is None:
            base = Version(major, minor, 0)
            if op == ">":
                return [_Comparator(">=", Version(major, minor + 1, 0))]
            if op == "<":
                return [_Comparator("<", base)]
            if op == ">=":
                return [_Comparator(">=", base)]
            if op == "<=":
                return [_Comparator("<", Version(major, minor + 1, 0))]
        return [_Comparator(op, Version(major, minor, patch, _parse_prerelease(pre)),
                            explicit_prerelease=pre is not None)]
    # 裸版本 / X 范围
    return _expand_partial_comparators(token)


def _parse_hyphen(segment: str) -> Tuple[str, bool]:
    """若为 hyphen 范围则返回（展开后的 AND 片段, True）。"""
    match = re.match(r"^\s*(\S+)\s+-\s+(\S+)\s*$", segment)
    if not match:
        return segment, False
    left_raw, right_raw = match.groups()
    left_parts = _parse_partial(left_raw)
    right_parts = _parse_partial(right_raw)
    if left_parts is None or right_parts is None:
        raise VersionError(f"非法 hyphen 范围: {segment!r}")
    lm, ln, lp, lpre = left_parts
    rm, rn, rp, rpre = right_parts
    if lm is None:
        raise VersionError(f"hyphen 范围下界非法: {segment!r}")
    lower = Version(lm, ln or 0, lp or 0, _parse_prerelease(lpre))
    if rn is None:
        upper_cmp = _Comparator("<", Version(rm + 1, 0, 0))
    elif rp is None:
        upper_cmp = _Comparator("<", Version(rm, rn + 1, 0))
    else:
        upper_cmp = _Comparator("<=", Version(rm, rn, rp, _parse_prerelease(rpre)),
                                explicit_prerelease=rpre is not None)
    comparators = [_Comparator(">=", lower, explicit_prerelease=lpre is not None), upper_cmp]
    return " ".join(f"{c.op}{c.version}" for c in comparators), True


@dataclass(frozen=True)
class VersionRange:
    """OR-AND 形式的版本范围：候选版本满足任一 comparator 集合即可。"""

    source: str
    sets: Tuple[Tuple[_Comparator, ...], ...]

    def satisfies(self, version: Version) -> bool:
        for comparators in self.sets:
            if self._satisfies_set(version, comparators):
                return True
        return False

    @staticmethod
    def _satisfies_set(version: Version, comparators: Tuple[_Comparator, ...]) -> bool:
        if version.is_prerelease:
            visible = any(
                c.version.triple == version.triple
                and (c.explicit_prerelease or c.op == "=")
                for c in comparators
            )
            if not visible:
                return False
        for c in comparators:
            if c.op == "=" and not (version == c.version):
                return False
            if c.op == ">=" and not (version >= c.version):
                return False
            if c.op == "<=" and not (version <= c.version):
                return False
            if c.op == ">" and not (version > c.version):
                return False
            if c.op == "<" and not (version < c.version):
                return False
        return True

    @staticmethod
    @lru_cache(maxsize=4096)
    def parse(text: str) -> "VersionRange":
        if not isinstance(text, str):
            raise VersionError(f"范围必须是字符串: {text!r}")
        source = text.strip()
        or_parts = re.split(r"\s*\|\|\s*", source)
        sets: List[Tuple[_Comparator, ...]] = []
        for or_part in or_parts:
            segment = or_part.strip()
            expanded, is_hyphen = _parse_hyphen(segment)
            if is_hyphen:
                segment = expanded
            comparators: List[_Comparator] = []
            for token in segment.split():
                comparators.extend(_parse_comparator_token(token))
            sets.append(tuple(comparators))
        return VersionRange(source=source, sets=tuple(sets))

    def __str__(self) -> str:
        return self.source


@lru_cache(maxsize=8192)
def satisfies(version_text: str, range_text: str) -> bool:
    """便捷入口：版本字符串是否满足范围字符串。"""
    return VersionRange.parse(range_text).satisfies(Version.parse(version_text))
