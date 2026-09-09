from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from droidock import DeviceRecord, DeviceStore, DroidockError


@pytest.fixture
def config_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("droidock.store.user_config_path", lambda name, **kwargs: tmp_path / name)
    return tmp_path / "droidock"


def test_new_installation_does_not_create_a_store_on_read(config_directory):
    store = DeviceStore()
    assert store.directory == config_directory
    assert not store.read().devices
    assert not config_directory.exists()


def test_profiles_and_preferences_persist_across_store_instances(config_directory):
    store = DeviceStore()

    def register(state):
        state.devices.append(DeviceRecord("saved-id", "Office XR", "SERIAL-ONE"))
        state.default_device = "saved-id"
        state.settings.auto_connect_on_start = False

    store.update(register)
    original = store.path.read_bytes()
    restored = DeviceStore()
    state = restored.read()
    assert restored.path == store.path
    assert state.devices[0].id == state.default_device == "saved-id"
    assert state.devices[0].name == "Office XR"
    assert not state.settings.auto_connect_on_start
    assert restored.path.read_bytes() == original


@pytest.mark.parametrize(
    "environment,explicit,expected",
    [(None, None, "droidock"), ("environment", None, "environment"), ("environment", "explicit", "explicit")],
)
def test_configuration_directory_precedence(
    config_directory, tmp_path, monkeypatch, environment, explicit, expected
):
    if environment:
        monkeypatch.setenv("DROIDOCK_HOME", str(tmp_path / environment))
    store = DeviceStore(tmp_path / explicit if explicit else None)
    assert store.directory == tmp_path / expected
    assert not store.read().devices


def test_invalid_store_is_preserved(tmp_path):
    store = DeviceStore(tmp_path)
    store.path.write_text('{"broken"', encoding="utf-8")
    with pytest.raises(DroidockError, match="not be overwritten"):
        store.update(lambda state: None)
    assert store.path.read_text(encoding="utf-8") == '{"broken"'


def test_concurrent_writers_do_not_lose_profiles(tmp_path):
    def add(index):
        def operation(state):
            state.devices.append(DeviceRecord(str(index), f"Phone {index}", f"SERIAL-{index}"))

        DeviceStore(tmp_path).update(operation)

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(add, range(15)))
    assert len(DeviceStore(tmp_path).read().devices) == 15
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["schema_version"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_per_application_stores_are_isolated(tmp_path):
    one, two = DeviceStore(tmp_path / "one"), DeviceStore(tmp_path / "two")
    one.update(lambda state: state.devices.append(DeviceRecord("1", "One", "SERIAL-ONE")))
    assert not two.read().devices


def test_unknown_schema_is_not_overwritten(tmp_path):
    store = DeviceStore(tmp_path)
    store.path.write_text('{"schema_version": 9}', encoding="utf-8")
    with pytest.raises(DroidockError):
        store.update(lambda state: None)
    assert json.loads(store.path.read_text())["schema_version"] == 9


def test_unwritable_storage_directory_reports_library_error(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.write_text("existing file", encoding="utf-8")
    with pytest.raises(DroidockError) as error:
        DeviceStore(occupied).update(lambda state: None)
    assert error.value.code == "store_unwritable"
    assert occupied.read_text(encoding="utf-8") == "existing file"


def test_invalid_device_model_is_rejected_without_overwrite(tmp_path):
    store = DeviceStore(tmp_path)
    store.update(lambda state: state.devices.append(DeviceRecord("1", "One", "SERIAL-ONE")))
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["devices"][0]["model"] = {"unexpected": "object"}
    original = json.dumps(data)
    store.path.write_text(original, encoding="utf-8")
    with pytest.raises(DroidockError) as error:
        store.update(lambda state: None)
    assert error.value.code == "invalid_store"
    assert store.path.read_text(encoding="utf-8") == original
