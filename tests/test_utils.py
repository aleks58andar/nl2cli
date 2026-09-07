"""Tests for utility functions."""

import pytest
from unittest.mock import patch, mock_open

from src.core.utils import (
    which, detect_init_system, detect_distro, 
    gather_host_facts, format_dict_for_prompt
)
from src.core.schema import InitSystem


def test_which():
    """Test which function."""
    # Test with a command that should exist
    result = which("python3")
    assert result is None or result.endswith("python3")
    
    # Test with a command that shouldn't exist
    result = which("nonexistent_command_12345")
    assert result is None


@patch('src.core.utils.which')
@patch('pathlib.Path.exists')
def test_detect_init_system(mock_exists, mock_which):
    """Test init system detection."""
    # Test systemd detection
    mock_which.return_value = "/usr/bin/systemctl"
    mock_exists.return_value = True
    
    result = detect_init_system()
    assert result == InitSystem.systemd
    
    # Test unknown system
    mock_which.return_value = None
    mock_exists.return_value = False
    
    result = detect_init_system()
    assert result == InitSystem.unknown


@patch('pathlib.Path.exists')
@patch('builtins.open', new_callable=mock_open, read_data='ID=ubuntu\nVERSION_ID="22.04"\n')
def test_detect_distro(mock_file, mock_exists):
    """Test distribution detection."""
    mock_exists.return_value = True
    
    distro_id, version = detect_distro()
    assert distro_id == "ubuntu"
    assert version == "22.04"


def test_format_dict_for_prompt():
    """Test dictionary formatting for prompts."""
    data = {
        "key1": "value1",
        "key2": ["item1", "item2"],
        "key3": []
    }
    
    result = format_dict_for_prompt(data)
    assert "key1: value1" in result
    assert "key2: item1, item2" in result
    assert "key3: (none)" in result


@patch('src.core.utils.detect_distro')
@patch('src.core.utils.detect_init_system')
@patch('src.core.utils.check_binary_availability')
@patch('src.core.utils.check_file_existence')
def test_gather_host_facts(mock_files, mock_binaries, mock_init, mock_distro):
    """Test host facts gathering."""
    mock_distro.return_value = ("ubuntu", "22.04")
    mock_init.return_value = InitSystem.systemd
    mock_binaries.return_value = ["systemctl", "apt"]
    mock_files.return_value = ["/etc/systemd"]
    
    facts = gather_host_facts()
    
    assert facts.distro_id == "ubuntu"
    assert facts.distro_version == "22.04"
    assert facts.init_system == InitSystem.systemd
    assert "systemctl" in facts.available_binaries
    assert "/etc/systemd" in facts.existing_files
