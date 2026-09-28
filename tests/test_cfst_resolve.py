from nodebench.probes.cfst import CfstProber, normalize_ip


class _T:
    def __init__(self, address, port=443):
        self.address = address
        self.port = port
        self.target_host = "octagoningly-vpn.pages.dev"
        self.tls = True
        self.params = {}
        self.remarks = ""
        self.item_id = "x"
        self.source_ids = ["cf"]
        self.raw_refs = []


def test_normalize_ip_keeps_hostname_lowercase():
    assert normalize_ip("CF.0sm.COM") == "cf.0sm.com"
    assert normalize_ip("104.17.29.227") == "104.17.29.227"


def test_resolve_target_ip_prefers_literal(tmp_path):
    prober = CfstProber(
        config=None,
        budget=None,
        run_id="r",
        runner_id="local:x",
        run_dir=tmp_path,
        binary=None,
    )
    ip = prober._resolve_target_ip(_T("104.17.29.227"))
    assert ip == "104.17.29.227"
    # cache hit returns same
    assert prober._resolve_target_ip(_T("104.17.29.227")) == "104.17.29.227"


def test_resolve_target_ip_resolves_hostname(monkeypatch, tmp_path):
    prober = CfstProber(
        config=None,
        budget=None,
        run_id="r",
        runner_id="local:x",
        run_dir=tmp_path,
        binary=None,
    )

    def fake_getaddrinfo(host, port, family=0, type=0):
        return [(2, 1, 6, "", ("203.0.113.9", port))]

    monkeypatch.setattr("nodebench.probes.cfst.socket.getaddrinfo", fake_getaddrinfo)
    ip = prober._resolve_target_ip(_T("cf.0sm.com"))
    assert ip == "203.0.113.9"
