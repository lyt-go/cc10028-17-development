# conftest.py：每个测试把数据产物目录隔离到临时路径，避免 jsonl 状态跨用例污染。
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services import base


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path, monkeypatch, request):
    data_root = tmp_path / "data"
    monkeypatch.setattr(base, "DATA_ROOT", data_root)
    # 测试模块以 from ... import DATA_ROOT 方式引用时，同步替换模块内名字
    module = sys.modules.get(request.module.__name__)
    if module is not None and hasattr(module, "DATA_ROOT"):
        monkeypatch.setattr(module, "DATA_ROOT", data_root, raising=False)
    yield data_root
