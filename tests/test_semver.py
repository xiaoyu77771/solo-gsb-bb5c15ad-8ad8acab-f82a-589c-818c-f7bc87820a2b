"""SemVer 解析器与范围匹配的单元测试。"""

import pytest

from app.semver import Version, VersionError, VersionRange, satisfies


class TestVersion:
    def test_parse_and_order(self):
        assert Version.parse("v1.2.3") == Version.parse("1.2.3")
        assert Version.parse("1.10.0") > Version.parse("1.9.0")
        assert Version.parse("2.0.0") > Version.parse("1.99.99")

    def test_prerelease_order(self):
        assert Version.parse("1.0.0-alpha") < Version.parse("1.0.0-alpha.1")
        assert Version.parse("1.0.0-alpha.1") < Version.parse("1.0.0-alpha.beta")
        assert Version.parse("1.0.0-beta") < Version.parse("1.0.0-rc.1")
        assert Version.parse("1.0.0-rc.1") < Version.parse("1.0.0")
        assert Version.parse("1.0.0") > Version.parse("1.0.0-rc.1")

    def test_invalid(self):
        with pytest.raises(VersionError):
            Version.parse("1.2")
        with pytest.raises(VersionError):
            Version.parse("01.2.3")
        with pytest.raises(VersionError):
            Version.parse("1.2.3-")

    def test_str_roundtrip(self):
        assert str(Version.parse("1.2.3-beta.1+exp.sha.5114f85")) == "1.2.3-beta.1+exp.sha.5114f85"


class TestRanges:
    @pytest.mark.parametrize(
        "version,text,expected",
        [
            ("1.2.3", "^1.0.0", True),
            ("2.0.0", "^1.0.0", False),
            ("1.0.0", "^1.0.0", True),
            ("0.0.3", "^0.0.3", True),
            ("0.0.4", "^0.0.3", False),
            ("0.2.9", "^0.2.3", True),
            ("0.3.0", "^0.2.3", False),
            ("0.5.9", "^0.5.0", True),
            ("1.4.9", "~1.4.0", True),
            ("1.5.0", "~1.4.0", False),
            ("1.2.9", "~1.2", True),
            ("1.3.0", "~1.2", False),
            ("1.2.3", "1.x", True),
            ("2.0.0", "1.x", False),
            ("1.2.9", "1.2", True),
            ("1.3.0", "1.2", False),
            ("1.5.0", "*", True),
            ("2.1.0", ">=1.0.0 <2.0.0", False),
            ("1.5.0", ">=1.0.0 <2.0.0", True),
            ("1.5.0", "1.0.0 - 2.0.0", True),
            ("2.0.0", "1.0.0 - 2.0.0", True),
            ("2.0.1", "1.0.0 - 2.0.0", False),
            ("1.0.0-rc.1", "^1.0.0", False),
            ("1.0.0-rc.1", ">=1.0.0-rc.1", True),
            ("1.0.0-rc.2", ">=1.0.0-rc.1", True),
            ("1.0.0", "1.0.0-rc.1 || ^1.0.0", True),
            ("2.5.0", "^1.0.0 || ^2.0.0", True),
            ("3.0.0", "^1.0.0 || ^2.0.0", False),
            ("1.2.3", "=1.2.3", True),
        ],
    )
    def test_satisfies(self, version, text, expected):
        assert satisfies(version, text) is expected

    def test_invalid_range(self):
        with pytest.raises(VersionError):
            VersionRange.parse("not-a-range")
