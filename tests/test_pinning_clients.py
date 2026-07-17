"""IPFS and Pinata clients against a mocked transport."""

import httpx
import pytest

from governance_service.clients import ipfs, pinata
from governance_service.clients.ipfs import IPFSClient, _parse_directory_response
from governance_service.clients.pinata import PinataClient
from governance_service.config import settings

DIRECTORY_RESPONSE = "\n".join(
    [
        '{"Name": "table.csv", "Hash": "bafy-table"}',
        '{"Name": "modelLinks.js", "Hash": "bafy-registry"}',
        '{"Name": "", "Hash": "bafy-root"}',
    ]
)


def _mock_transport(monkeypatch, module, handler):
    real_client = httpx.Client

    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(module.httpx, "Client", factory)


def test_parse_directory_response_returns_root_cid():
    assert _parse_directory_response(DIRECTORY_RESPONSE) == "bafy-root"
    assert _parse_directory_response('{"Name": "only-file", "Hash": "x"}') is None


def test_ipfs_requires_api_url():
    with pytest.raises(ValueError, match="IPFS_API_URL"):
        IPFSClient(api_url="")


def test_pin_directory_returns_root_cid(monkeypatch):
    def handler(request):
        assert "wrap-with-directory=true" in str(request.url)
        return httpx.Response(200, text=DIRECTORY_RESPONSE)

    _mock_transport(monkeypatch, ipfs, handler)
    cid = IPFSClient(api_url="http://ipfs.test:5001").pin_directory(
        {"table.csv": b"csv", "modelLinks.js": b"js"}
    )
    assert cid == "bafy-root"


def test_pin_directory_rejects_empty_input():
    assert IPFSClient(api_url="http://ipfs.test:5001").pin_directory({}) is None


def test_pin_directory_returns_none_after_exhausted_retries(monkeypatch):
    monkeypatch.setattr(settings, "http_retry_base_delay", 0)

    def handler(request):
        return httpx.Response(500, text="node down")

    _mock_transport(monkeypatch, ipfs, handler)
    cid = IPFSClient(api_url="http://ipfs.test:5001").pin_directory({"a": b"x"})
    assert cid is None


def test_pinata_requires_credentials():
    with pytest.raises(ValueError, match="Pinata credentials"):
        PinataClient(api_key="", api_secret="")


def test_pinata_pin_by_cid_success(monkeypatch):
    def handler(request):
        assert request.url.path == "/pinning/pinByHash"
        assert request.headers["pinata_api_key"] == "key"
        return httpx.Response(200, json={"id": "queued"})

    _mock_transport(monkeypatch, pinata, handler)
    assert PinataClient(api_key="key", api_secret="secret").pin_by_cid("bafy-root")


def test_pinata_pin_by_cid_failure_is_nonfatal(monkeypatch):
    monkeypatch.setattr(settings, "http_retry_base_delay", 0)

    def handler(request):
        return httpx.Response(500, text="unavailable")

    _mock_transport(monkeypatch, pinata, handler)
    assert not PinataClient(api_key="key", api_secret="secret").pin_by_cid("bafy-root")


def test_pinata_direct_upload_returns_root_cid(monkeypatch):
    def handler(request):
        assert request.url.path == "/pinning/pinFileToIPFS"
        assert b'name="file"; filename="bundle/table.csv"' in request.content
        return httpx.Response(200, json={"IpfsHash": "bafy-direct"})

    _mock_transport(monkeypatch, pinata, handler)
    cid = PinataClient(api_key="key", api_secret="secret").pin_directory(
        {"table.csv": b"csv"}, name="test-pin"
    )
    assert cid == "bafy-direct"
