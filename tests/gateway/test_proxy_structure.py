"""Architecture guards for the streaming gateway proxy."""

import ast
import inspect

import ctrlrtn.gateway.proxy as facade


def test_gateway_proxy_package_is_an_import_facade():
    tree = ast.parse(inspect.getsource(facade))
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert facade.__all__ == [
        "DecideFn",
        "FallbackFn",
        "ProviderResolveFn",
        "TerminalError",
        "_forward_request_headers",
        "_forward_response_headers",
        "proxy_pass_through",
    ]


def test_gateway_proxy_capabilities_stay_in_their_modules():
    assert facade.TerminalError.__module__ == ("ctrlrtn.gateway.proxy.models")
    assert facade._forward_request_headers.__module__ == (
        "ctrlrtn.gateway.proxy.headers"
    )
    assert facade.proxy_pass_through.__module__ == (
        "ctrlrtn.gateway.proxy.handler"
    )
