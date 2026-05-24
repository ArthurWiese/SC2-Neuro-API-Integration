from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from watchdog.events import FileSystemEventHandler

import xml.etree.ElementTree as ET
import os
import time
import tempfile

if TYPE_CHECKING:
    from SC2_integration import TerminalApp


class BankFileEventHandler(FileSystemEventHandler):
    def __init__(self, integration: TerminalApp, target_file_name: str) -> None:
        self._integration = integration
        self._target_file_name = target_file_name.lower()

    def on_modified(self, event):
        self._notify_if_target(event)

    def on_created(self, event):
        self._notify_if_target(event)

    def on_moved(self, event):
        self._notify_if_target(event)

    def _notify_if_target(self, event) -> None:
        if getattr(event, "is_directory", False):
            return

        src_path = getattr(event, "src_path", "") or getattr(event, "dest_path", "")
        if not src_path:
            return

        if Path(src_path).name.lower() != self._target_file_name:
            return

        self._integration._notify_bank_file_changed(src_path)


def parse_bank_file(bank_file: Path) -> dict[str, dict[str, Any]]:
    tree = ET.parse(bank_file)
    root = tree.getroot()
    parsed: dict[str, dict[str, Any]] = {}

    for section in root.findall("Section"):
        section_name = section.get("name")
        if not section_name:
            continue

        section_dict: dict[str, Any] = {}
        for key in section.findall("Key"):
            key_name = key.get("name")
            if not key_name:
                continue
            value_node = key.find("Value")
            section_dict[key_name] = parse_bank_value(value_node)

        parsed[section_name] = section_dict

    return parsed


def parse_bank_value(value_node: ET.Element | None) -> Any:
    if value_node is None:
        return None

    if "flag" in value_node.attrib:
        return value_node.attrib["flag"] == "1"
    if "int" in value_node.attrib:
        try:
            return int(value_node.attrib["int"])
        except ValueError:
            return value_node.attrib["int"]
    if "fixed" in value_node.attrib:
        try:
            return float(value_node.attrib["fixed"])
        except ValueError:
            return value_node.attrib["fixed"]
    if "string" in value_node.attrib:
        return value_node.attrib["string"]
    if "text" in value_node.attrib:
        return value_node.attrib["text"]

    if value_node.attrib:
        return dict(value_node.attrib)

    return (value_node.text or "").strip() if value_node.text else None



def write_bank_values(bank_file: Path, updates: dict[str, dict[str, Any]]) -> None:
    # Attempt to apply updates with an atomic replace to avoid partial writes
    attempts = 0
    max_attempts = 2
    backoff = 0.03

    while True:
        attempts += 1
        try:
            tree = ET.parse(bank_file)
            root = tree.getroot()

            for section_name, key_values in updates.items():
                section_node = _find_or_create_section(root, section_name)
                for key_name, value in key_values.items():
                    key_node = _find_or_create_key(section_node, key_name)
                    value_node = key_node.find("Value")
                    if value_node is None:
                        value_node = ET.SubElement(key_node, "Value")
                    _set_bank_value_node(value_node, value, key_name)

            ET.indent(tree, space="    ")

            # Write to a temp file in same directory then atomically replace
            dirpath = bank_file.parent
            with tempfile.NamedTemporaryFile("wb", dir=dirpath, delete=False, suffix=".tmp") as tf:
                temp_path = Path(tf.name)
                # write tree to the temp file path
                tf.close()
                tree.write(str(temp_path), encoding="utf-8", xml_declaration=True)

            os.replace(str(temp_path), str(bank_file))
            return
        except (ET.ParseError, OSError):
            if attempts >= max_attempts:
                raise
            time.sleep(backoff)
            continue


def _find_or_create_section(root: ET.Element, section_name: str) -> ET.Element:
    for section in root.findall("Section"):
        if section.get("name") == section_name:
            return section
    return ET.SubElement(root, "Section", {"name": section_name})


def _find_or_create_key(section_node: ET.Element, key_name: str) -> ET.Element:
    for key in section_node.findall("Key"):
        if key.get("name") == key_name:
            return key
    return ET.SubElement(section_node, "Key", {"name": key_name})


def _set_bank_value_node(value_node: ET.Element, value: Any, key_name: str | None = None) -> None:
    value_node.attrib.clear()
    value_node.text = None

    if isinstance(value, bool):
        value_node.set("flag", "1" if value else "0")
        return
    if isinstance(value, int):
        value_node.set("int", str(value))
        return
    if isinstance(value, float):
        value_node.set("fixed", str(value))
        return

    value_node.set("string", str(value))


def deactivate_everything(bank_file: Path) -> None:
    """Set all <Value flag="1"/> to <Value flag="0"/> in the bank file.

    When preserve_in_mission is True, the game_state/in_mission flag is left unchanged.
    """
    attempts = 0
    max_attempts = 2
    backoff = 0.03

    while True:
        attempts += 1
        try:
            tree = ET.parse(bank_file)
            root = tree.getroot()
            changed = False

            for section in root.findall("Section"):
                for key_node in section.findall("Key"):
                    value_node = key_node.find("Value")
                    if value_node is None or value_node.get("flag") != "1":
                        continue
                    value_node.set("flag", "0")
                    changed = True

            if changed:
                ET.indent(tree, space="    ")

                dirpath = bank_file.parent
                with tempfile.NamedTemporaryFile("wb", dir=dirpath, delete=False, suffix=".tmp") as tf:
                    temp_path = Path(tf.name)
                    tf.close()
                    tree.write(str(temp_path), encoding="utf-8", xml_declaration=True)

                os.replace(str(temp_path), str(bank_file))
            return
        except (ET.ParseError, OSError):
            if attempts >= max_attempts:
                raise
            time.sleep(backoff)
            continue


def clear_game_context_flags(bank_file: Path) -> None:
    """Clear any Value flag="1" inside the 'game_context' Section."""
    try:
        attempts = 0
        max_attempts = 2
        backoff = 0.03

        while True:
            attempts += 1
            try:
                tree = ET.parse(bank_file)
                root = tree.getroot()
                changed = False
                for section in root.findall("Section"):
                    if section.get("name") == "game_context":
                        for key_node in section.findall("Key"):
                            v = key_node.find("Value")
                            if v is not None and v.get("flag") == "1":
                                v.set("flag", "0")
                                changed = True
                        break
                if changed:
                    ET.indent(tree, space="    ")

                    dirpath = bank_file.parent
                    with tempfile.NamedTemporaryFile("wb", dir=dirpath, delete=False, suffix=".tmp") as tf:
                        temp_path = Path(tf.name)
                        tf.close()
                        tree.write(str(temp_path), encoding="utf-8", xml_declaration=True)

                    os.replace(str(temp_path), str(bank_file))
                return
            except (ET.ParseError, OSError):
                if attempts >= max_attempts:
                    raise
                time.sleep(backoff)
                continue
    except Exception:
        raise


def clear_force_action_section(bank_file: Path) -> None:
    """Remove all keys from the 'force_action' section."""
    attempts = 0
    max_attempts = 2
    backoff = 0.03

    while True:
        attempts += 1
        try:
            tree = ET.parse(bank_file)
            root = tree.getroot()
            changed = False

            for section in root.findall("Section"):
                if section.get("name") == "force_action":
                    keys = list(section.findall("Key"))
                    if keys:
                        for key_node in keys:
                            section.remove(key_node)
                        changed = True
                    break

            if changed:
                ET.indent(tree, space="    ")

                dirpath = bank_file.parent
                with tempfile.NamedTemporaryFile("wb", dir=dirpath, delete=False, suffix=".tmp") as tf:
                    temp_path = Path(tf.name)
                    tf.close()
                    tree.write(str(temp_path), encoding="utf-8", xml_declaration=True)

                os.replace(str(temp_path), str(bank_file))

            return
        except (ET.ParseError, OSError):
            if attempts >= max_attempts:
                raise
            time.sleep(backoff)
