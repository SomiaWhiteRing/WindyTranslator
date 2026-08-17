import importlib.util
import sys
import tempfile
from pathlib import Path


ENTRY = Path(__file__).with_name("install_mojibake_rtp.py")
SPEC = importlib.util.spec_from_file_location("install_mojibake_rtp", ENTRY)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def main():
    sys.path.append(str(Path(__file__).parents[2]))
    assert (Path(__file__).parents[2] / "modules" / "RTPCollection" / "2000fix.zip").is_file()

    with tempfile.TemporaryDirectory() as temp:
        calls = []

        class FakeRtp:
            RTP_COLLECTION_DIR = "wrong"

            @staticmethod
            def install_rtp_files(project, archives):
                calls.append((project, archives, FakeRtp.RTP_COLLECTION_DIR))
                return True

        original_loader = MODULE._load_rtp_module
        MODULE._load_rtp_module = lambda: FakeRtp
        assert MODULE.install(Path(temp)) is True
        MODULE._load_rtp_module = original_loader
        assert calls == [
            (temp, ["2000fix.zip"], str(Path(__file__).parents[2] / "modules" / "RTPCollection"))
        ]

        project = Path(temp) / "missing"
        try:
            MODULE.install(project)
        except ValueError as exc:
            assert "游戏目录不存在" in str(exc)
        else:
            raise AssertionError("missing project must fail")
    print("install_mojibake_rtp self-check passed")


if __name__ == "__main__":
    main()
