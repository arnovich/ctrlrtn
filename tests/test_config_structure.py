"""Architecture guards for decomposed runtime configuration."""

import ast
import inspect

import ctrlrtn.config as facade


def test_config_package_is_an_import_facade():
    tree = ast.parse(inspect.getsource(facade))
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert facade.__all__ == ["ConfigError", "Settings", "load_settings"]


def test_config_capabilities_stay_in_their_modules():
    assert facade.ConfigError.__module__ == "ctrlrtn.config.models"
    assert facade.Settings.__module__ == "ctrlrtn.config.models"
    assert facade.load_settings.__module__ == "ctrlrtn.config.loader"
